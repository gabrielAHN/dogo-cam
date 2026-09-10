import importlib.util
import sys
import types
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "ky004-control.py"


def load_module():
    previous = sys.modules.get("lgpio")
    sys.modules["lgpio"] = types.ModuleType("lgpio")
    try:
        spec = importlib.util.spec_from_file_location("ky004_control", MODULE_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("lgpio", None)
        else:
            sys.modules["lgpio"] = previous


class SwitchModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.control = load_module()

    def test_auto_mode_uses_always_on_when_pin_follows_internal_pulls(self):
        self.assertEqual(
            self.control.resolve_switch_mode("auto", pull_down=0, pull_up=1),
            "always-on",
        )

    def test_auto_mode_uses_gpio_for_external_pull_up(self):
        self.assertEqual(
            self.control.resolve_switch_mode("auto", pull_down=1, pull_up=1),
            "gpio",
        )

    def test_auto_mode_uses_gpio_for_external_pull_down(self):
        self.assertEqual(
            self.control.resolve_switch_mode("auto", pull_down=0, pull_up=0),
            "gpio",
        )

    def test_auto_mode_fails_safe_to_gpio_for_unstable_detection(self):
        self.assertEqual(
            self.control.resolve_switch_mode("auto", pull_down=None, pull_up=1),
            "gpio",
        )

    def test_explicit_mode_overrides_detection(self):
        self.assertEqual(
            self.control.resolve_switch_mode("always-on", pull_down=1, pull_up=1),
            "always-on",
        )
        self.assertEqual(
            self.control.resolve_switch_mode("gpio", pull_down=0, pull_up=1),
            "gpio",
        )

    def test_invalid_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "SWITCH_MODE"):
            self.control.resolve_switch_mode("invalid", pull_down=0, pull_up=1)

    def test_stable_gpio_value_returns_shared_sample(self):
        self.assertEqual(self.control.stable_gpio_value([1, 1, 1]), 1)
        self.assertEqual(self.control.stable_gpio_value([0, 0, 0]), 0)

    def test_stable_gpio_value_rejects_mixed_samples(self):
        self.assertIsNone(self.control.stable_gpio_value([0, 1, 0]))

    def test_read_pull_value_reclaims_pin_and_samples_it(self):
        calls = []

        class FakeGPIO:
            def gpio_free(self, handle, pin):
                calls.append(("free", handle, pin))

            def gpio_claim_input(self, handle, pin, pull):
                calls.append(("claim", handle, pin, pull))

            def gpio_read(self, handle, pin):
                calls.append(("read", handle, pin))
                return 1

        original_gpio = getattr(self.control, "lgpio")
        try:
            setattr(self.control, "lgpio", FakeGPIO())
            value = self.control.read_pull_value(4, 17, 32, samples=3, settle=0)
        finally:
            setattr(self.control, "lgpio", original_gpio)

        self.assertEqual(value, 1)
        self.assertEqual(calls[0], ("free", 4, 17))
        self.assertEqual(calls[1], ("claim", 4, 17, 32))
        self.assertEqual(calls.count(("read", 4, 17)), 3)

    def test_detect_switch_mode_identifies_a_floating_pin(self):
        reads = []

        def fake_read(handle, pin, pull, samples=5, settle=0.05):
            reads.append((handle, pin, pull))
            return {64: 0, 32: 1}[pull]

        original_gpio = getattr(self.control, "lgpio")
        original_read = getattr(self.control, "read_pull_value")
        original_mode = getattr(self.control, "SWITCH_MODE")
        try:
            setattr(
                self.control,
                "lgpio",
                types.SimpleNamespace(SET_PULL_DOWN=64, SET_PULL_UP=32),
            )
            setattr(self.control, "read_pull_value", fake_read)
            setattr(self.control, "SWITCH_MODE", "auto")

            self.assertEqual(self.control.detect_switch_mode(4), "always-on")
            self.assertEqual(reads, [(4, 17, 64), (4, 17, 32)])
        finally:
            setattr(self.control, "lgpio", original_gpio)
            setattr(self.control, "read_pull_value", original_read)
            setattr(self.control, "SWITCH_MODE", original_mode)

    def test_run_always_on_starts_and_reconciles_camera(self):
        applied = []
        moments = iter([0.0, 6.0, 6.0])
        original_apply = getattr(self.control, "apply_state")
        original_time_module = getattr(self.control, "time")
        fake_time = types.SimpleNamespace(
            time=lambda: next(moments),
            sleep=lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        try:
            setattr(
                self.control,
                "apply_state",
                lambda state, force_led=False: applied.append((state, force_led)),
            )
            setattr(self.control, "time", fake_time)

            self.control.run_always_on()
        finally:
            setattr(self.control, "apply_state", original_apply)
            setattr(self.control, "time", original_time_module)

        self.assertEqual(
            applied,
            [
                (self.control.SWITCH_ON_VALUE, True),
                (self.control.SWITCH_ON_VALUE, False),
            ],
        )

    def test_explicit_always_on_does_not_open_gpio(self):
        calls = []

        class FailGPIO:
            def gpiochip_open(self, _chip):
                raise AssertionError("GPIO must not be opened in always-on mode")

        original_gpio = getattr(self.control, "lgpio")
        original_mode = getattr(self.control, "SWITCH_MODE")
        original_runner = getattr(self.control, "run_always_on")
        try:
            setattr(self.control, "lgpio", FailGPIO())
            setattr(self.control, "SWITCH_MODE", "always-on")
            setattr(self.control, "run_always_on", lambda: calls.append("always-on"))

            self.control.main()
        finally:
            setattr(self.control, "lgpio", original_gpio)
            setattr(self.control, "SWITCH_MODE", original_mode)
            setattr(self.control, "run_always_on", original_runner)

        self.assertEqual(calls, ["always-on"])

    def test_auto_always_on_closes_gpio_handle(self):
        closed = []
        calls = []

        class FakeGPIO:
            SET_PULL_UP = 32
            SET_PULL_DOWN = 64

            def gpiochip_open(self, _chip):
                return 42

            def gpiochip_close(self, handle):
                closed.append(handle)

        pull_values = iter([0, 1])
        original_gpio = getattr(self.control, "lgpio")
        original_mode = getattr(self.control, "SWITCH_MODE")
        original_reader = getattr(self.control, "read_pull_value")
        original_runner = getattr(self.control, "run_always_on")
        try:
            setattr(self.control, "lgpio", FakeGPIO())
            setattr(self.control, "SWITCH_MODE", "auto")
            setattr(self.control, "read_pull_value", lambda *_args: next(pull_values))
            setattr(self.control, "run_always_on", lambda: calls.append("always-on"))

            self.control.main()
        finally:
            setattr(self.control, "lgpio", original_gpio)
            setattr(self.control, "SWITCH_MODE", original_mode)
            setattr(self.control, "read_pull_value", original_reader)
            setattr(self.control, "run_always_on", original_runner)

        self.assertEqual(closed, [42])
        self.assertEqual(calls, ["always-on"])

    def test_auto_external_pull_routes_to_gpio_monitoring(self):
        pulls = []
        closed = []
        sleep_calls = []
        applied = []

        class FakeGPIO:
            SET_PULL_UP = 32
            SET_PULL_DOWN = 64

            def gpiochip_open(self, _chip):
                return 9

            def gpio_read(self, _handle, _pin):
                return self_control.SWITCH_ON_VALUE

            def gpiochip_close(self, handle):
                closed.append(handle)

        def fake_pull(_handle, _pin, pull, **_kwargs):
            pulls.append(pull)
            return 0

        def stop_monitoring(_seconds):
            sleep_calls.append(None)
            if len(sleep_calls) == 4:
                raise KeyboardInterrupt

        self_control = self.control
        original_gpio = getattr(self.control, "lgpio")
        original_mode = getattr(self.control, "SWITCH_MODE")
        original_reader = getattr(self.control, "read_pull_value")
        original_apply = getattr(self.control, "apply_state")
        original_runner = getattr(self.control, "run_always_on")
        original_time = getattr(self.control, "time")
        try:
            setattr(self.control, "lgpio", FakeGPIO())
            setattr(self.control, "SWITCH_MODE", "auto")
            setattr(self.control, "read_pull_value", fake_pull)
            setattr(
                self.control,
                "apply_state",
                lambda state, force_led=False: applied.append((state, force_led)),
            )
            setattr(
                self.control,
                "run_always_on",
                lambda: self.fail("external pull must use GPIO mode"),
            )
            setattr(
                self.control,
                "time",
                types.SimpleNamespace(time=lambda: 0.0, sleep=stop_monitoring),
            )

            self.control.main()
        finally:
            setattr(self.control, "lgpio", original_gpio)
            setattr(self.control, "SWITCH_MODE", original_mode)
            setattr(self.control, "read_pull_value", original_reader)
            setattr(self.control, "apply_state", original_apply)
            setattr(self.control, "run_always_on", original_runner)
            setattr(self.control, "time", original_time)

        self.assertEqual(pulls, [64, 32, 32])
        self.assertEqual(applied, [(self.control.SWITCH_ON_VALUE, True)])
        self.assertEqual(closed, [9])

    def test_gpio_mode_finishes_with_pull_up_and_closes_handle(self):
        claims = []
        closed = []
        sleep_calls = []

        class FakeGPIO:
            SET_PULL_UP = 32
            SET_PULL_DOWN = 64

            def gpiochip_open(self, _chip):
                return 7

            def gpio_free(self, _handle, _pin):
                return None

            def gpio_claim_input(self, handle, pin, pull):
                claims.append((handle, pin, pull))

            def gpio_read(self, _handle, _pin):
                return self_control.SWITCH_ON_VALUE

            def gpiochip_close(self, handle):
                closed.append(handle)

        self_control = self.control
        original_gpio = getattr(self.control, "lgpio")
        original_mode = getattr(self.control, "SWITCH_MODE")
        original_apply = getattr(self.control, "apply_state")
        original_sleep = getattr(self.control.time, "sleep")
        try:
            setattr(self.control, "lgpio", FakeGPIO())
            setattr(self.control, "SWITCH_MODE", "gpio")
            setattr(self.control, "apply_state", lambda *_args, **_kwargs: None)

            def stop_monitoring(_seconds):
                sleep_calls.append(None)
                if len(sleep_calls) == 5:
                    raise KeyboardInterrupt

            setattr(self.control.time, "sleep", stop_monitoring)
            self.control.main()
        finally:
            setattr(self.control, "lgpio", original_gpio)
            setattr(self.control, "SWITCH_MODE", original_mode)
            setattr(self.control, "apply_state", original_apply)
            setattr(self.control.time, "sleep", original_sleep)

        self.assertEqual(claims[-1], (7, self.control.SWITCH_PIN, 32))
        self.assertEqual(closed, [7])


if __name__ == "__main__":
    unittest.main()
