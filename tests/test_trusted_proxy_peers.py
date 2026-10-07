"""Trusted Remote-* headers only count when they come from the SSO proxy.

The jg-casa reverse proxy (Traefik + Authelia -> nginx) reaches the Pi over
Nebula and forwards the Authelia identity as Remote-User / Remote-Groups. The
app used to believe those headers from ANY peer, and gunicorn listened on
0.0.0.0:5000, so any device on the home Wi-Fi could send
`Remote-Groups: admins` and skip Authelia, including servo control.

These tests pin the fix:
  * Remote-* headers (and X-Forwarded-*) are honoured only when the TCP peer
    is in TRUSTED_PROXY_ADDRS (default: loopback only);
  * optional DOGCAM_VIEW_GROUPS re-checks the viewer permission;
  * the systemd unit no longer binds a wildcard address;
  * the watchdog keeps passing the view-group check.

Runs off-Pi with stubbed camera libs, like the other suites. No real servo or
camera is touched: the servo controller is a Mock.

Run:  python3 -m unittest tests.test_trusted_proxy_peers
"""

import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_camera_features import _install_hardware_stubs  # noqa: E402

LAN_PEER = "192.168.50.50"
PROXY_PEER = "192.168.100.1"
PROXY_ALLOWLIST = "127.0.0.1,::1,192.168.100.1"
MOVE = {"axis": "servo2", "direction": "left"}
FORGED_OPERATOR = {"Remote-User": "intruder", "Remote-Groups": "admins,dogo_operators,dogo_viewers"}


class TrustedProxyPeerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Same live-like env as the Pi: both proxy-trust flags on.
        os.environ["TRUST_PROXY_AUTH_HEADERS"] = "1"
        os.environ["TRUST_PROXY_HEADERS"] = "1"
        os.environ["SECRET_KEY"] = "test"
        os.environ["DOGCAM_CONTROL_GROUPS"] = "admin,admins,dogo_operators"
        os.environ.pop("TRUSTED_PROXY_ADDRS", None)
        os.environ.pop("DOGCAM_VIEW_GROUPS", None)
        _install_hardware_stubs()
        for name in ("dogcam_stream", "servo_control_rpigpio"):
            sys.modules.pop(name, None)
        import dogcam_stream

        cls.mod = dogcam_stream

    def setUp(self):
        self.servo = Mock()
        self.servo.move_servo2.return_value = (True, 80, True, True)
        self.servo.reset_to_home.return_value = True
        self.servo.get_position.return_value = {
            "servo1": 90, "servo2": 90,
            "can_servo1_up": True, "can_servo1_down": True,
            "can_servo2_left": True, "can_servo2_right": True,
        }
        state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(state_dir.cleanup)
        self.restart_camera = Mock(return_value=True)
        patches = [
            patch.object(self.mod, "servo_controller", self.servo),
            patch.object(self.mod, "servo_available", True),
            # Camera-setting writes go to a temp file and never restart anything.
            patch.object(self.mod, "CAMERA_VIEW_STATE_FILE", os.path.join(state_dir.name, "view.json")),
            patch.object(self.mod, "camera_available", True),
            patch.object(self.mod, "restart_camera", self.restart_camera),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _client(self, peer):
        client = self.mod.app.test_client()
        client.environ_base["REMOTE_ADDR"] = peer
        return client

    def _env(self, **values):
        clean = {k: v for k, v in values.items() if v is not None}
        ctx = patch.dict(os.environ, clean, clear=False)
        ctx.start()
        self.addCleanup(ctx.stop)
        for key, value in values.items():
            if value is None:
                old = os.environ.pop(key, None)
                if old is not None:
                    self.addCleanup(os.environ.__setitem__, key, old)

    # ---- RED in the audit: forged identity from the LAN ----
    def test_lan_peer_with_forged_remote_user_is_not_signed_in(self):
        self._env(TRUSTED_PROXY_ADDRS=PROXY_ALLOWLIST)
        response = self._client(LAN_PEER).get("/camera_status", headers=FORGED_OPERATOR)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_lan_peer_with_forged_operator_groups_cannot_move_the_servo(self):
        self._env(TRUSTED_PROXY_ADDRS=PROXY_ALLOWLIST)
        client = self._client(LAN_PEER)
        move = client.post("/servo/move", json=MOVE, headers=FORGED_OPERATOR)
        reset = client.post("/servo/reset", headers=FORGED_OPERATOR)
        self.assertNotEqual(move.status_code // 100, 2, move.status_code)
        self.assertNotEqual(reset.status_code // 100, 2, reset.status_code)
        self.servo.move_servo2.assert_not_called()
        self.servo.reset_to_home.assert_not_called()

    def test_lan_peer_cannot_change_camera_settings(self):
        self._env(TRUSTED_PROXY_ADDRS=PROXY_ALLOWLIST)
        response = self._client(LAN_PEER).post(
            "/camera/view", json={"view": "upside_down"}, headers=FORGED_OPERATOR
        )
        self.assertEqual(response.status_code, 302)
        self.restart_camera.assert_not_called()
        self.assertFalse(os.path.exists(self.mod.CAMERA_VIEW_STATE_FILE))

    def test_lan_peer_cannot_borrow_the_proxy_address_via_x_forwarded_for(self):
        self._env(TRUSTED_PROXY_ADDRS=PROXY_ALLOWLIST)
        headers = dict(FORGED_OPERATOR, **{"X-Forwarded-For": PROXY_PEER})
        response = self._client(LAN_PEER).get("/camera_status", headers=headers)
        self.assertEqual(response.status_code, 302)

    def test_forwarded_host_from_untrusted_peer_is_ignored(self):
        self._env(TRUSTED_PROXY_ADDRS=PROXY_ALLOWLIST)
        response = self._client(LAN_PEER).get(
            "/", headers={"X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "https"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("evil.example", response.headers["Location"])

    def test_default_allowlist_is_loopback_only(self):
        self._env(TRUSTED_PROXY_ADDRS=None)
        lan = self._client(LAN_PEER).get("/camera_status", headers=FORGED_OPERATOR)
        overlay = self._client(PROXY_PEER).get("/camera_status", headers=FORGED_OPERATOR)
        self.assertEqual(lan.status_code, 302)
        self.assertEqual(overlay.status_code, 302)

    # ---- GREEN paths that must keep working ----
    def test_sso_proxy_peer_is_trusted(self):
        self._env(TRUSTED_PROXY_ADDRS=PROXY_ALLOWLIST)
        response = self._client(PROXY_PEER).get(
            "/camera_status", headers={"Remote-User": "gabriel", "Remote-Groups": "dogo_viewers"}
        )
        self.assertEqual(response.status_code, 200)

    def test_sso_operator_through_proxy_can_still_move_the_servo(self):
        self._env(TRUSTED_PROXY_ADDRS=PROXY_ALLOWLIST)
        response = self._client(PROXY_PEER).post(
            "/servo/move",
            json=MOVE,
            headers={"Remote-User": "gabriel", "Remote-Groups": "dogo_viewers,dogo_operators"},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.servo.move_servo2.assert_called_once_with("left")

    def test_proxy_viewer_without_control_group_still_gets_403(self):
        self._env(TRUSTED_PROXY_ADDRS=PROXY_ALLOWLIST)
        response = self._client(PROXY_PEER).post(
            "/servo/move",
            json=MOVE,
            headers={"Remote-User": "jen", "Remote-Groups": "dogo_viewers"},
        )
        self.assertEqual(response.status_code, 403)
        self.servo.move_servo2.assert_not_called()

    def test_loopback_watchdog_is_trusted_by_default(self):
        self._env(TRUSTED_PROXY_ADDRS=None)
        response = self._client("127.0.0.1").get("/camera_status", headers={"Remote-User": "watchdog"})
        self.assertEqual(response.status_code, 200)

    def test_cidr_entries_and_ipv4_mapped_peers_match(self):
        self._env(TRUSTED_PROXY_ADDRS="127.0.0.1, 192.168.100.0/30")
        mapped = self._client("::ffff:192.168.100.1").get("/camera_status", headers={"Remote-User": "a"})
        outside = self._client("192.168.100.10").get("/camera_status", headers={"Remote-User": "a"})
        self.assertEqual(mapped.status_code, 200)
        self.assertEqual(outside.status_code, 302)

    def test_invalid_allowlist_entries_are_ignored_not_widened(self):
        self._env(TRUSTED_PROXY_ADDRS="not-an-ip, 127.0.0.1")
        lan = self._client(LAN_PEER).get("/camera_status", headers=FORGED_OPERATOR)
        local = self._client("127.0.0.1").get("/camera_status", headers={"Remote-User": "a"})
        self.assertEqual(lan.status_code, 302)
        self.assertEqual(local.status_code, 200)

    def test_forwarded_headers_still_apply_for_the_trusted_proxy(self):
        self._env(TRUSTED_PROXY_ADDRS=PROXY_ALLOWLIST)
        response = self._client(PROXY_PEER).get(
            "/", headers={"X-Forwarded-Host": "dogo.jg-casa.com", "X-Forwarded-Proto": "https"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("next=https://dogo.jg-casa.com/", response.headers["Location"])

    def test_local_password_session_is_unchanged(self):
        self._env(TRUSTED_PROXY_ADDRS=PROXY_ALLOWLIST)
        client = self._client("127.0.0.1")
        with client.session_transaction() as local_session:
            local_session["logged_in"] = True
        self.assertEqual(client.get("/camera_status").status_code, 200)

    # ---- defence in depth: viewer group re-check ----
    def test_view_groups_unset_keeps_any_proxy_user(self):
        self._env(TRUSTED_PROXY_ADDRS=PROXY_ALLOWLIST, DOGCAM_VIEW_GROUPS=None)
        response = self._client(PROXY_PEER).get(
            "/camera_status", headers={"Remote-User": "u", "Remote-Groups": "users"}
        )
        self.assertEqual(response.status_code, 200)

    def test_view_groups_reject_proxy_user_without_viewer_group(self):
        self._env(TRUSTED_PROXY_ADDRS=PROXY_ALLOWLIST, DOGCAM_VIEW_GROUPS="dogo_viewers")
        client = self._client(PROXY_PEER)
        headers = {"Remote-User": "u", "Remote-Groups": "users,admins"}
        for method, path in (("GET", "/"), ("GET", "/camera_status"), ("GET", "/video_feed"),
                             ("GET", "/camera/view"), ("GET", "/login"), ("POST", "/servo/move")):
            with self.subTest(path=path):
                response = client.open(path, method=method, headers=headers, json=MOVE if method == "POST" else None)
                self.assertEqual(response.status_code, 403)
        self.servo.move_servo2.assert_not_called()

    def test_view_groups_accept_viewer(self):
        self._env(TRUSTED_PROXY_ADDRS=PROXY_ALLOWLIST, DOGCAM_VIEW_GROUPS="dogo_viewers")
        response = self._client(PROXY_PEER).get(
            "/camera_status", headers={"Remote-User": "jen", "Remote-Groups": "users,dogo_viewers"}
        )
        self.assertEqual(response.status_code, 200)

    def test_view_groups_do_not_block_local_password_session(self):
        self._env(TRUSTED_PROXY_ADDRS=PROXY_ALLOWLIST, DOGCAM_VIEW_GROUPS="dogo_viewers")
        client = self._client("127.0.0.1")
        with client.session_transaction() as local_session:
            local_session["logged_in"] = True
        self.assertEqual(client.get("/camera_status").status_code, 200)


def _unit_directives(text):
    directives = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";", "[")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        directives.setdefault(key.strip(), []).append(value.strip())
    return directives


class ServiceBindTest(unittest.TestCase):
    """The service must never listen on a wildcard address again."""

    SERVICE = ROOT / "service_startup" / "dog-stream-flask.service"
    WILDCARDS = {"", "0.0.0.0", "::", "[::]", "*"}

    def _binds(self):
        directives = _unit_directives(self.SERVICE.read_text())
        argv = shlex.split(directives["ExecStart"][-1])
        binds = [argv[i + 1] for i, arg in enumerate(argv) if arg in ("--bind", "-b")]
        binds += [arg.split("=", 1)[1] for arg in argv if arg.startswith("--bind=")]
        return argv, binds

    def test_gunicorn_binds_only_loopback_and_the_nebula_address(self):
        _, binds = self._binds()
        self.assertEqual(sorted(binds), ["127.0.0.1:5000", "192.168.100.10:5000"])
        for bind in binds:
            host = bind.rsplit(":", 1)[0]
            self.assertNotIn(host, self.WILDCARDS, bind)

    def test_gunicorn_parses_the_same_addresses(self):
        try:
            from gunicorn.config import Config
        except ImportError:  # pragma: no cover - gunicorn lives in the Pi venv
            self.skipTest("gunicorn not installed")
        argv, _ = self._binds()
        app_index = next(i for i, arg in enumerate(argv) if arg.endswith("gunicorn"))
        cfg = Config()
        args = cfg.parser().parse_args(argv[app_index + 1:])
        cfg.set("bind", args.bind)
        self.assertEqual(sorted(cfg.address), [("127.0.0.1", 5000), ("192.168.100.10", 5000)])

    def test_unit_waits_for_and_retries_until_nebula_is_up(self):
        directives = _unit_directives(self.SERVICE.read_text())
        after = " ".join(directives.get("After", []))
        wants = " ".join(directives.get("Wants", []))
        self.assertIn("nebula.service", after)
        self.assertIn("nebula.service", wants)
        # Binding the overlay address fails (EADDRNOTAVAIL) until nebula has
        # configured it; systemd must keep retrying instead of giving up.
        self.assertEqual(directives.get("Restart", [""])[-1], "always")
        self.assertEqual(directives.get("StartLimitIntervalSec", [""])[-1], "0")

    def test_env_example_documents_a_narrow_allowlist(self):
        env_example = (ROOT / ".env.example").read_text()
        lines = [l for l in env_example.splitlines() if l.startswith("TRUSTED_PROXY_ADDRS=")]
        self.assertEqual(len(lines), 1)
        value = lines[0].split("=", 1)[1].split("#", 1)[0]
        for entry in filter(None, (e.strip() for e in value.split(","))):
            self.assertFalse(entry.endswith("/0"), entry)
            self.assertNotIn(entry, self.WILDCARDS)
        self.assertIn("DOGCAM_VIEW_GROUPS=", env_example)


class WatchdogViewGroupTest(unittest.TestCase):
    """dogcam-watchdog.sh must still reach /stream_health when view groups are on."""

    def _run(self, extra_env):
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp)
            log = bindir / "curl.log"
            stubs = {
                # Record every curl argv (NUL separated) and report HTTP 200.
                "curl": '#!/bin/sh\nfor a in "$@"; do printf "%s\\0" "$a"; done >> "$CURL_LOG"; printf "\\n" >> "$CURL_LOG"; printf 200\n',
                "systemctl": "#!/bin/sh\necho systemctl \"$@\" >> \"$CURL_LOG.systemctl\"\n",
                "logger": "#!/bin/sh\nexit 0\n",
                "sleep": "#!/bin/sh\nexit 0\n",
            }
            for name, body in stubs.items():
                path = bindir / name
                path.write_text(body)
                path.chmod(path.stat().st_mode | stat.S_IEXEC)
            env = {
                "PATH": f"{bindir}:/usr/bin:/bin",
                "CURL_LOG": str(log),
                "PORT": "5000",
            }
            env.update(extra_env)
            subprocess.run(["bash", str(ROOT / "dogcam-watchdog.sh")], env=env, check=True, timeout=30)
            calls = [c.split("\0") for c in log.read_text().split("\n") if c]
            restarted = (bindir / "curl.log.systemctl").exists()
        return calls, restarted

    def _health_headers(self, calls):
        health = [c for c in calls if any(a.endswith("/stream_health") for a in c)]
        self.assertEqual(len(health), 1, calls)
        call = health[0]
        return [call[i + 1] for i, a in enumerate(call) if a == "-H"]

    def test_watchdog_sends_first_view_group(self):
        calls, restarted = self._run({"DOGCAM_VIEW_GROUPS": " dogo_viewers , dogo_operators"})
        headers = self._health_headers(calls)
        self.assertIn("Remote-User: watchdog", headers)
        self.assertIn("Remote-Groups: dogo_viewers", headers)
        self.assertFalse(restarted)

    def test_watchdog_without_view_groups_sends_no_group_header(self):
        calls, restarted = self._run({})
        headers = self._health_headers(calls)
        self.assertIn("Remote-User: watchdog", headers)
        self.assertFalse(any(h.startswith("Remote-Groups") for h in headers), headers)
        self.assertFalse(restarted)

    def test_watchdog_talks_to_loopback(self):
        calls, _ = self._run({})
        urls = [a for c in calls for a in c if a.startswith("http")]
        self.assertTrue(urls)
        for url in urls:
            self.assertTrue(url.startswith("http://127.0.0.1:5000/"), url)


if __name__ == "__main__":
    unittest.main()
