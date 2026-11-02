"""dogcam-watchdog.sh must leave dog-stream alone while the camera is off by remote.

ky004-control.py persists the retained ``jg-casa/dogo-cam/power/set`` value to
DOGCAM_REMOTE_POWER_FILE and stops dog-stream while it is OFF. The watchdog runs
as root every 30 s; without this guard it sees a dead app, restarts dog-stream,
ky004 stops it again, and Away mode never holds.

Every test runs the real script under bash with fake ``curl``, ``systemctl``,
``logger`` and ``sleep`` executables first on PATH. Each fake appends its argv
to a log, so the tests see exactly which commands the script ran.
"""

import importlib.util
import logging
import os
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WATCHDOG = ROOT / "dogcam-watchdog.sh"
KY004_PATH = ROOT / "ky004-control.py"
DEFAULT_REMOTE_POWER_FILE = "/var/lib/dogcam/remote-power"

# Append "arg1\0arg2\0...\n" for each call to $FAKE_LOG_DIR/<tool>.log.
_RECORD = '{ for a in "$@"; do printf \'%s\\0\' "$a"; done; printf \'\\n\'; } >> "$FAKE_LOG_DIR/TOOL.log"\n'
FAKES = {
    "curl": (
        "#!/bin/sh\n"
        + _RECORD.replace("TOOL", "curl")
        # Like curl -w '%{http_code}': 000 and a non-zero exit when nothing answers.
        + 'code="${FAKE_CURL_CODE:-200}"\n'
        + "printf '%s' \"$code\"\n"
        + '[ "$code" = 000 ] && exit 7\n'
        + "exit 0\n"
    ),
    "systemctl": "#!/bin/sh\n" + _RECORD.replace("TOOL", "systemctl") + "exit 0\n",
    "logger": "#!/bin/sh\n" + _RECORD.replace("TOOL", "logger") + "exit 0\n",
    # The liveness loop sleeps 3 s between attempts; skip the wait.
    "sleep": "#!/bin/sh\n" + _RECORD.replace("TOOL", "sleep") + "exit 0\n",
}

LIVENESS_URL = "http://127.0.0.1:5000/"
HEALTH_URL = "http://127.0.0.1:5000/stream_health"
RESTART = ["restart", "dog-stream.service"]

_MISSING = object()


def load_control():
    """Import ky004-control.py by path with a stub lgpio (no GPIO, no MQTT)."""
    previous = sys.modules.get("lgpio")
    sys.modules["lgpio"] = types.ModuleType("lgpio")
    try:
        spec = importlib.util.spec_from_file_location("ky004_control_watchdog_parity", KY004_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("lgpio", None)
        else:
            sys.modules["lgpio"] = previous


class WatchdogRemotePowerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.bin = self.tmp / "bin"
        self.logs = self.tmp / "logs"
        self.bin.mkdir()
        self.logs.mkdir()
        for name, body in FAKES.items():
            path = self.bin / name
            path.write_text(body)
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        self.power_file = self.tmp / "remote-power"
        self.stall_stamp = self.tmp / "dogcam-stall-since"

    # -- helpers ------------------------------------------------------------
    def write_power(self, content):
        if isinstance(content, bytes):
            self.power_file.write_bytes(content)
        else:
            self.power_file.write_text(content)

    def run_watchdog(self, curl_code="000", power=_MISSING, extra_env=None):
        """Run the real script; return (returncode, {tool: [argv, ...]})."""
        if power is not _MISSING:
            self.write_power(power)
        for old in self.logs.iterdir():
            old.unlink()
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "FAKE_LOG_DIR": str(self.logs),
            "FAKE_CURL_CODE": curl_code,
            "PORT": "5000",
            "DOGCAM_REMOTE_POWER_FILE": str(self.power_file),
            # Keep the tests off the real /tmp/dogcam-stall-since.
            "DOGCAM_STALL_STAMP": str(self.stall_stamp),
        }
        env.update(extra_env or {})
        result = subprocess.run(
            ["bash", str(WATCHDOG)], env=env, capture_output=True, text=True, timeout=30
        )
        calls = {}
        for tool in FAKES:
            log = self.logs / f"{tool}.log"
            lines = log.read_text().split("\n") if log.exists() else []
            calls[tool] = [line.split("\0")[:-1] for line in lines if line]
        self.last_stderr = result.stderr
        return result.returncode, calls

    def assertSkipped(self, returncode, calls):
        self.assertEqual(returncode, 0, self.last_stderr)
        self.assertEqual(calls["curl"], [], "no HTTP check while the camera is off by remote")
        self.assertEqual(calls["systemctl"], [], "no restart while the camera is off by remote")
        self.assertEqual(calls["sleep"], [])
        self.assertEqual(len(calls["logger"]), 1, calls["logger"])
        argv = calls["logger"][0]
        self.assertEqual(argv[:2], ["-t", "dogcam-watchdog"], argv)
        self.assertIn("off by remote", argv[-1].lower(), argv)

    def assertRestartedAsUnresponsive(self, returncode, calls):
        self.assertEqual(returncode, 0, self.last_stderr)
        liveness = [c for c in calls["curl"] if c and c[-1] == LIVENESS_URL]
        self.assertEqual(len(liveness), 3, calls["curl"])
        self.assertEqual(calls["systemctl"], [RESTART])
        self.assertEqual(len(calls["logger"]), 1, calls["logger"])
        self.assertEqual(calls["logger"][0][:2], ["-t", "dogcam-watchdog"])
        self.assertIn("unresponsive", calls["logger"][0][-1])

    # -- remote OFF ---------------------------------------------------------
    def test_remote_off_skips_every_check_and_restart_even_when_the_app_is_dead(self):
        self.assertSkipped(*self.run_watchdog(curl_code="000", power="OFF\n"))

    def test_remote_off_with_a_live_app_still_sends_no_request(self):
        self.assertSkipped(*self.run_watchdog(curl_code="200", power="OFF\n"))

    def test_remote_off_is_matched_after_strip_and_case_insensitively(self):
        for content in ("off", " OFF\n", "Off", "oFf", "OFF", "\toff \r\n", "\n\n  OFF  \n\n"):
            with self.subTest(content=content):
                self.assertSkipped(*self.run_watchdog(curl_code="000", power=content))

    def test_remote_off_drops_a_stale_stall_record(self):
        # A stall noted before the camera went off must not count against the
        # in-process recovery window once the camera is back on.
        self.stall_stamp.write_text("1\n")
        self.assertSkipped(*self.run_watchdog(curl_code="000", power="OFF\n"))
        self.assertFalse(self.stall_stamp.exists())

    # -- remote ON / no usable file: behaviour unchanged ---------------------
    def test_remote_on_keeps_the_unresponsive_restart(self):
        for content in ("ON\n", "on", " On \n"):
            with self.subTest(content=content):
                self.assertRestartedAsUnresponsive(*self.run_watchdog(curl_code="000", power=content))

    def test_remote_on_with_a_healthy_app_checks_health_and_does_nothing(self):
        returncode, calls = self.run_watchdog(curl_code="200", power="ON\n")
        self.assertEqual(returncode, 0, self.last_stderr)
        self.assertEqual([c[-1] for c in calls["curl"]], [LIVENESS_URL, HEALTH_URL])
        self.assertEqual(calls["systemctl"], [])
        self.assertEqual(calls["logger"], [])

    def test_missing_file_and_unresponsive_app_still_restarts(self):
        self.assertFalse(self.power_file.exists())
        self.assertRestartedAsUnresponsive(*self.run_watchdog(curl_code="000"))

    def test_garbage_or_empty_file_keeps_the_restart(self):
        for content in ("", "\n", "   ", "STANDBY", "OFFLINE", "OF F", "OFF\nON", "0", "false", b"\xff\xfeOFF"):
            with self.subTest(content=content):
                self.assertRestartedAsUnresponsive(*self.run_watchdog(curl_code="000", power=content))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can read a mode-000 file")
    def test_unreadable_file_keeps_the_restart(self):
        self.write_power("OFF\n")
        self.power_file.chmod(0)
        self.addCleanup(self.power_file.chmod, 0o600)
        self.assertRestartedAsUnresponsive(*self.run_watchdog(curl_code="000"))

    def test_directory_in_place_of_the_file_keeps_the_restart(self):
        self.power_file.mkdir()
        self.assertRestartedAsUnresponsive(*self.run_watchdog(curl_code="000"))

    # -- same file and same parsing as ky004-control.py ----------------------
    def test_default_file_is_the_one_ky004_persists(self):
        script = WATCHDOG.read_text()
        self.assertIn("${DOGCAM_REMOTE_POWER_FILE:-" + DEFAULT_REMOTE_POWER_FILE + "}", script)
        self.assertEqual(load_control().DEFAULT_REMOTE_POWER_FILE, DEFAULT_REMOTE_POWER_FILE)

    def test_skip_decision_matches_ky004_parsing(self):
        control = load_control()
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        contents = [
            "OFF", "OFF\n", "off", " OFF\n", "\toff \r\n", "Off\n\n",
            "ON", "on\n", "", "\n", "STANDBY", "OFFLINE", "OF F", "OFF\nON", "0",
        ]
        for content in [_MISSING, *contents]:
            with self.subTest(content=content):
                if content is _MISSING:
                    if self.power_file.exists():
                        self.power_file.unlink()
                else:
                    self.write_power(content)
                ky004_off = control.load_remote_power(str(self.power_file)) == "OFF"
                _returncode, calls = self.run_watchdog(curl_code="000")
                watchdog_skipped = calls["systemctl"] == [] and calls["curl"] == []
                self.assertEqual(watchdog_skipped, ky004_off)


if __name__ == "__main__":
    unittest.main(verbosity=2)
