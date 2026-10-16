"""Mobile-friendly UI and the per-device "Show title" switch.

Template checks always run (stub hardware, Flask test client). The browser
layout checks run when Playwright is installed, on Chromium (system Chrome or
Playwright's build) and WebKit when they can launch, e.g.:

    uv run --no-project --with flask --with python-dotenv --with playwright \
        python -m unittest tests.test_mobile_ui -v

Phone rule set: one row of 44px icon-only buttons (Home, Logout, settings,
fullscreen) next to a compact status pill, the title on one line under it, and
a settings sheet that fits a 393x852 phone without scrolling. Desktop keeps
its text buttons.
"""

import contextlib
import os
import re
import socket
import sys
import threading
import unittest
from pathlib import Path
from socketserver import ThreadingMixIn
from unittest.mock import patch
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_demand_streaming import _install_hardware_stubs  # noqa: E402

ENV = {
    "TRUST_PROXY_AUTH_HEADERS": "1",
    "SECRET_KEY": "test",
    "DAYNIGHT_AUTO": "1",
    "DAYNIGHT_CHECK_INTERVAL": "60",
    "STREAM_STALL_CHECK_INTERVAL": "60",
    "DOGCAM_CONTROL_GROUPS": "admins",
    "DOG_NAME": "Kotaro",
    "DOGCAM_HOME_URL": "https://portal.example/",
    "DOGCAM_LOGOUT_URL": "https://auth.example/logout",
}
ADMIN = {"Remote-User": "test", "Remote-Groups": "admins"}


def _load_app():
    _install_hardware_stubs()
    for name in ("dogcam_stream", "servo_control_rpigpio"):
        sys.modules.pop(name, None)
    import dogcam_stream
    return dogcam_stream


class MobileTemplateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environ = patch.dict(os.environ, ENV, clear=False)
        cls.environ.start()
        cls.mod = _load_app()

    @classmethod
    def tearDownClass(cls):
        cls.mod.cleanup()
        cls.environ.stop()

    def assertMarker(self, marker, html, present=True):
        self.assertEqual(marker in html, present, f"{marker!r} present={not present}")

    def _html(self, **flags):
        with patch.multiple(self.mod, **flags) if flags else contextlib.nullcontext():
            with self.mod.app.test_client() as client:
                response = client.get("/", headers=ADMIN)
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_show_title_switch_is_in_the_settings_sheet(self):
        html = self._html()
        sheet = html.split('id="servo-controls"', 1)[1]
        for marker in ('id="toggle-title"', 'role="switch"', "Show title"):
            self.assertMarker(marker, sheet)

    def test_title_preference_is_per_device_and_applied_before_paint(self):
        html = self._html()
        head = html.split("</head>", 1)[0]
        # Read from localStorage in <head> so a hidden title never flashes.
        for marker in ("dogcam.showTitle", "title-hidden", "localStorage"):
            self.assertMarker(marker, head)

    def test_settings_and_title_switch_render_without_camera_features(self):
        html = self._html(camera_available=False, servo_available=False,
                          DAYNIGHT_ENABLED=False)
        for marker in ('id="settings-btn"', 'id="servo-controls-modal"', 'id="toggle-title"'):
            self.assertMarker(marker, html)
        for marker in ('id="panel-camera-view"', 'id="panel-daynight"', 'id="panel-manual"'):
            self.assertMarker(marker, html, present=False)
        self.assertIsNone(re.search(r"#settings-btn\s*\{[^}]*display:\s*none", html))

    def test_icon_buttons_keep_accessible_names(self):
        html = self._html()
        for marker in ('href="https://portal.example/">Home</a>',
                       'href="https://auth.example/logout">Logout</a>',
                       'aria-label="Camera settings"', 'aria-label="Toggle fullscreen"',
                       'aria-label="Close camera settings"'):
            self.assertMarker(marker, html)

    def test_phone_overlays_respect_horizontal_safe_areas(self):
        # viewport-fit=cover lets content run under a landscape notch, so the
        # centred overlays and the settings sheet must keep clear of it.
        html = self._html()
        for selector in ("#stream-title", "#temp", "#servo-controls-modal"):
            rule = re.search(re.escape(selector) + r"\s*\{[^}]*safe-area-inset-(?:left|right)[^}]*\}", html)
            self.assertIsNotNone(rule, f"{selector} ignores the horizontal safe area")


# --------------------------------------------------------------------------
# Browser layout checks
# --------------------------------------------------------------------------

try:  # pragma: no cover - depends on the developer machine
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

SYSTEM_CHROME = os.getenv(
    "DOGCAM_TEST_CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
FEED_SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" width="1296" height="972">'
            b'<rect width="1296" height="972" fill="#556"/></svg>')
SERVO_JSON = (b'{"servo1": 90, "servo2": 30, "can_servo1_up": true, "can_servo1_down": true,'
              b' "can_servo2_left": true, "can_servo2_right": true}')


class _ThreadedWSGI(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass


def _fixture(app):
    """Inject the SSO identity and stub the hardware-only endpoints."""
    def wrapped(environ, start_response):
        environ["HTTP_REMOTE_USER"] = "test"
        environ["HTTP_REMOTE_GROUPS"] = "admins"
        path = environ.get("PATH_INFO", "")
        if path == "/video_feed":
            start_response("200 OK", [("Content-Type", "image/svg+xml")])
            return [FEED_SVG]
        if path.startswith("/servo/"):
            start_response("200 OK", [("Content-Type", "application/json")])
            return [SERVO_JSON]
        return app(environ, start_response)
    return wrapped


MEASURE = r"""
() => {
  const r = (n) => n.getBoundingClientRect();
  const shown = (n) => !!n && r(n).width > 0 && r(n).height > 0 &&
      getComputedStyle(n).display !== 'none' && getComputedStyle(n).visibility !== 'hidden';
  const lines = (n) => { const range = document.createRange(); range.selectNodeContents(n);
      return new Set([...range.getClientRects()].filter(x => x.width > 0).map(x => Math.round(x.top))).size; };
  const box = (n) => { const b = r(n); return {x: b.left, y: b.top, w: b.width, h: b.height,
      right: b.right, bottom: b.bottom, mid: b.top + b.height / 2}; };
  const named = (n) => (n.getAttribute('aria-label') || n.textContent).trim();
  const actions = [...document.querySelectorAll('#top-actions > a, #top-actions > button')].filter(shown);
  const overlay = [document.querySelector('h1'), document.getElementById('stream-status'),
                   ...actions, document.getElementById('temp')].filter(shown);
  const overlaps = [];
  for (let i = 0; i < overlay.length; i++) for (let j = i + 1; j < overlay.length; j++) {
    const a = r(overlay[i]), b = r(overlay[j]);
    const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left);
    const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
    if (ox > 0.5 && oy > 0.5) overlaps.push(named(overlay[i]) + ' X ' + named(overlay[j]));
  }
  const h1 = document.querySelector('h1');
  const status = document.getElementById('stream-status');
  const temp = document.getElementById('temp');
  const visibleSpans = (n) => [...n.querySelectorAll('span')].filter(s => shown(s) && r(s).width > 1);
  const visibleText = (n) => { const spans = visibleSpans(n);
      return spans.length ? spans.map(s => s.textContent).join(' ') : n.innerText.trim(); };
  const tempText = visibleSpans(temp)[0] || temp;
  return {
    actions: actions.map(n => ({name: named(n), ...box(n)})),
    status: {...box(status), text: visibleText(status)},
    h1: shown(h1) ? {...box(h1), lines: lines(h1)} : null,
    temp: {...box(temp), text: visibleText(temp), lines: lines(tempText)},
    overlaps,
    pageFits: document.documentElement.scrollWidth <= innerWidth,
  };
}
"""

SIMULATE_POLLS = r"""
() => {
  const swap = (id, text) => {
    const el = document.getElementById(id);
    el.innerHTML = text;
    el.dispatchEvent(new CustomEvent('htmx:afterSwap', {bubbles: true, detail: {target: el, elt: el}}));
  };
  swap('stream-status', '⚪ Stream Cooling Down');
  swap('temp', 'Room Temp: 27.50°C (81.50°F) | Humidity: 51.0%');
}
"""

SHEET = r"""
() => {
  const sheet = document.getElementById('servo-controls');
  const r = (n) => n.getBoundingClientRect();
  const shown = (n) => r(n).width > 0 && r(n).height > 0 && getComputedStyle(n).display !== 'none';
  const targets = [...sheet.querySelectorAll('button, input:not(.switch-row input), label.switch-row'),
                   document.querySelector('.close-modal')].filter(shown);
  const s = r(sheet);
  return {
    fits: sheet.scrollHeight <= sheet.clientHeight + 1,
    scrollHeight: sheet.scrollHeight, clientHeight: sheet.clientHeight,
    inViewport: s.top >= 0 && s.bottom <= innerHeight + 0.5 && s.left >= 0 && s.right <= innerWidth + 0.5,
    small: targets.filter(n => r(n).height < 44 - 0.5 || (n.tagName !== 'INPUT' && r(n).width < 44 - 0.5))
                  .map(n => (n.id || n.className || n.tagName) + ' ' + Math.round(r(n).width) + 'x' + Math.round(r(n).height)),
  };
}
"""

PHONES = [("phone-320", 320, 568), ("phone-393", 393, 852), ("landscape-852", 852, 393)]
DESKTOP = ("desktop-1280", 1280, 800)


class MobileLayoutBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # DOGCAM_REQUIRE_BROWSER=1: missing Playwright or either engine is a
        # failure (WebKit is the closest stand-in for iPhone Safari).
        require = bool(os.getenv("DOGCAM_REQUIRE_BROWSER"))
        if sync_playwright is None:
            if require:
                raise RuntimeError("DOGCAM_REQUIRE_BROWSER is set but Playwright is not installed")
            raise unittest.SkipTest("Playwright is not installed")
        cls.environ = patch.dict(os.environ, ENV, clear=False)
        cls.environ.start()
        cls.mod = _load_app()
        cls.flags = patch.multiple(cls.mod, servo_available=True)
        cls.flags.start()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        cls.port = sock.getsockname()[1]
        sock.close()
        cls.server = make_server("127.0.0.1", cls.port, _fixture(cls.mod.app),
                                 server_class=_ThreadedWSGI, handler_class=_QuietHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}/"
        cls.pw = sync_playwright().start()
        cls.engines = []
        missing = []
        for name, launch in (
            ("chromium", lambda: cls.pw.chromium.launch(executable_path=SYSTEM_CHROME)
             if os.path.exists(SYSTEM_CHROME) else cls.pw.chromium.launch()),
            ("webkit", lambda: cls.pw.webkit.launch()),
        ):
            try:
                cls.engines.append((name, launch()))
            except Exception as error:  # pragma: no cover - engine not installed
                missing.append(name)
                print(f"[mobile-ui] {name} unavailable: {str(error).splitlines()[0]}")
        if (require and missing) or not cls.engines:
            cls.tearDownClass()
            if require:
                raise RuntimeError(f"DOGCAM_REQUIRE_BROWSER is set but {', '.join(missing)} did not launch")
            raise unittest.SkipTest("no Playwright browser could launch")

    @classmethod
    def tearDownClass(cls):
        for _, browser in getattr(cls, "engines", []):
            browser.close()
        cls.pw.stop()
        cls.server.shutdown()
        cls.flags.stop()
        cls.mod.cleanup()
        cls.environ.stop()

    def _page(self, browser, width, height, touch=True, polls=True):
        context = browser.new_context(viewport={"width": width, "height": height}, has_touch=touch)
        # Offline + deterministic: no CDN htmx/fonts; polls are simulated.
        context.route("**/*", lambda route: route.abort()
                      if not route.request.url.startswith(self.base) else route.continue_())
        page = context.new_page()
        page.goto(self.base, wait_until="load")
        if polls:
            page.evaluate(SIMULATE_POLLS)
        return context, page

    def test_phone_top_bar_is_one_row_of_icon_buttons(self):
        for engine, browser in self.engines:
            for label, width, height in PHONES:
                with self.subTest(engine=engine, size=label):
                    context, page = self._page(browser, width, height)
                    try:
                        m = page.evaluate(MEASURE)
                        names = [a["name"] for a in m["actions"]]
                        self.assertEqual(names, ["Home", "Logout", "Camera settings", "Toggle fullscreen"])
                        for action in m["actions"]:
                            self.assertGreaterEqual(action["w"], 43.5, action)
                            self.assertGreaterEqual(action["h"], 43.5, action)
                            self.assertLessEqual(action["w"], 48, f"{action['name']} is not icon-only: {action}")
                            self.assertLessEqual(action["right"], width, action)
                        sizes = {(round(a["w"]), round(a["h"])) for a in m["actions"]}
                        self.assertEqual(sizes, {(44, 44)}, f"top buttons differ in size: {m['actions']}")
                        mids = [a["mid"] for a in m["actions"]] + [m["status"]["mid"]]
                        self.assertLess(max(mids) - min(mids), 6, f"top bar is not one row: {m}")
                        icons = page.evaluate("""() => [...document.querySelectorAll('#top-actions a')].map(a => {
                            const before = getComputedStyle(a, '::before');
                            return (before.maskImage && before.maskImage !== 'none' ? before.maskImage
                                    : before.webkitMaskImage) || 'none'; })""")
                        self.assertEqual(len(icons), 2)
                        for icon in icons:
                            self.assertIn("image/svg+xml", icon, "Home/Logout need a monochrome SVG icon")
                        self.assertLessEqual(m["status"]["w"], 120, m["status"])
                        self.assertNotIn("Stream Cooling Down", m["status"]["text"])
                        self.assertIn("Cooling", m["status"]["text"])
                        self.assertIn("27.5°C", m["temp"]["text"])
                        self.assertEqual(m["h1"]["lines"], 1, m["h1"])
                        self.assertGreaterEqual(m["h1"]["x"], 0)
                        self.assertLessEqual(m["h1"]["right"], width)
                        self.assertEqual(m["temp"]["lines"], 1, m["temp"])
                        self.assertLessEqual(m["temp"]["right"], width)
                        self.assertEqual(m["overlaps"], [])
                        self.assertTrue(m["pageFits"])
                    finally:
                        context.close()

    def test_phone_top_bar_fits_before_the_first_status_poll(self):
        # Until htmx answers (or if its CDN script fails) the pill shows the
        # server-rendered placeholder; it must already be the compact form.
        for engine, browser in self.engines:
            for label, width, height in PHONES:
                with self.subTest(engine=engine, size=label):
                    context, page = self._page(browser, width, height, polls=False)
                    try:
                        m = page.evaluate(MEASURE)
                        self.assertEqual(m["overlaps"], [])
                        self.assertLessEqual(m["status"]["w"], 120, m["status"])
                        self.assertIn("Checking", m["status"]["text"])
                        self.assertTrue(m["pageFits"])
                    finally:
                        context.close()

    def test_desktop_keeps_text_buttons_and_full_status(self):
        # A short desktop window (e.g. a laptop with a docked console) is not
        # a phone: it keeps the desktop controls too.
        sizes = (DESKTOP, ("desktop-short", 1280, 480), ("desktop-short-narrow", 900, 450))
        for engine, browser in self.engines:
            for label, width, height in sizes:
                with self.subTest(engine=engine, size=label):
                    context, page = self._page(browser, width, height, touch=False)
                    try:
                        m = page.evaluate(MEASURE)
                        by_name = {a["name"]: a for a in m["actions"]}
                        self.assertEqual(list(by_name),
                                         ["Home", "Logout", "Camera settings", "Toggle fullscreen"])
                        self.assertGreater(by_name["Home"]["w"], 52)
                        self.assertGreater(by_name["Logout"]["w"], 52)
                        self.assertIn("Stream Cooling Down", m["status"]["text"])
                        self.assertEqual(m["h1"]["lines"], 1, m["h1"])
                        self.assertIn("Room Temp: 27.50°C (81.50°F) | Humidity: 51.0%",
                                      page.inner_text("#temp"))
                        self.assertEqual(m["overlaps"], [])
                    finally:
                        context.close()

    def test_phone_settings_sheet_fits_and_targets_stay_44px(self):
        # A 393x852 phone shows every panel without scrolling; smaller or
        # landscape screens may scroll the sheet but must keep it on screen.
        sizes = ((393, 852, True), (320, 568, False), (852, 393, False))
        for engine, browser in self.engines:
            for width, height, must_fit in sizes:
                with self.subTest(engine=engine, size=f"{width}x{height}"):
                    context, page = self._page(browser, width, height)
                    try:
                        page.tap("#settings-btn")
                        page.wait_for_selector("#servo-controls-modal.active")
                        sheet = page.evaluate(SHEET)
                        if must_fit:
                            self.assertTrue(sheet["fits"], sheet)
                        self.assertTrue(sheet["inViewport"], sheet)
                        self.assertEqual(sheet["small"], [])
                    finally:
                        context.close()

    def test_show_title_switch_hides_title_per_device(self):
        for engine, browser in self.engines:
            with self.subTest(engine=engine):
                context, page = self._page(browser, 393, 852)
                try:
                    self.assertTrue(page.is_visible("h1"))
                    page.tap("#settings-btn")
                    switch = page.locator("#toggle-title")
                    self.assertTrue(switch.is_checked())
                    page.tap("label.switch-row")
                    self.assertFalse(switch.is_checked())
                    self.assertFalse(page.is_visible("h1"))
                    page.reload(wait_until="load")
                    self.assertFalse(page.is_visible("h1"), "choice not remembered")
                    page.tap("#settings-btn")
                    self.assertFalse(page.locator("#toggle-title").is_checked())
                    page.keyboard.press("Escape")
                    self.assertFalse(page.is_visible("#servo-controls-modal"))
                    self.assertEqual(page.evaluate("document.activeElement.id"), "settings-btn")
                    # Another device (fresh storage) still shows the title.
                    other, other_page = self._page(browser, 393, 852)
                    try:
                        self.assertTrue(other_page.is_visible("h1"))
                    finally:
                        other.close()
                    page.tap("#settings-btn")
                    page.tap("label.switch-row")
                    self.assertTrue(page.is_visible("h1"))
                finally:
                    context.close()

    def test_phone_pseudo_fullscreen_keeps_exit_button(self):
        for engine, browser in self.engines:
            with self.subTest(engine=engine):
                context, page = self._page(browser, 393, 852)
                try:
                    page.evaluate("enterMobileFullscreen()")
                    self.assertEqual(page.text_content("#fullscreen-btn"), "✕", "button still shows enter glyph")
                    # The other top buttons hide at once (no visibility transition).
                    self.assertEqual(page.evaluate(
                        "getComputedStyle(document.getElementById('settings-btn')).visibility"), "hidden")
                    hit = page.evaluate("""() => {
                        const b = document.getElementById('fullscreen-btn').getBoundingClientRect();
                        const hit = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2);
                        return {id: hit && hit.id, w: b.width, h: b.height,
                                opacity: getComputedStyle(document.getElementById('top-actions')).opacity};
                    }""")
                    self.assertEqual(hit["id"], "fullscreen-btn", hit)
                    self.assertFalse(page.is_visible("#settings-btn"))
                    page.tap("#fullscreen-btn")
                    self.assertFalse(page.evaluate("document.body.classList.contains('mobile-fullscreen')"))
                    self.assertEqual(page.text_content("#fullscreen-btn"), "⛶")
                finally:
                    context.close()

    def test_settings_dialog_keeps_keyboard_focus_inside(self):
        inside = "() => document.getElementById('servo-controls').contains(document.activeElement)"
        for engine, browser in self.engines:
            for touch, (width, height) in ((True, (393, 852)), (False, DESKTOP[1:])):
                with self.subTest(engine=engine, size=f"{width}x{height}"):
                    context, page = self._page(browser, width, height, touch=touch)
                    try:
                        page.click("#settings-btn")
                        page.wait_for_selector("#servo-controls-modal.active")
                        count = page.evaluate("""() => document.getElementById('servo-controls')
                            .querySelectorAll('button, input').length""")
                        for key in ("Tab", "Shift+Tab"):
                            for step in range(count + 2):
                                page.keyboard.press(key)
                                self.assertTrue(page.evaluate(inside),
                                                f"{key} #{step + 1} left the dialog: "
                                                + page.evaluate("document.activeElement.outerHTML.slice(0, 80)"))
                        page.keyboard.press("Escape")
                        self.assertEqual(page.evaluate("document.activeElement.id"), "settings-btn")
                    finally:
                        context.close()

    def test_feed_reconnects_after_back_forward_cache_restore(self):
        src = "() => document.getElementById('video-feed').getAttribute('src')"
        for engine, browser in self.engines:
            with self.subTest(engine=engine):
                context, page = self._page(browser, 393, 852)
                try:
                    page.wait_for_function(f"({src})() !== null")
                    page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}))")
                    self.assertIsNone(page.evaluate(src), "pagehide must release the stream")
                    # A late error from the closed stream must not reopen it.
                    page.evaluate("() => document.getElementById('video-feed').dispatchEvent(new Event('error'))")
                    page.wait_for_timeout(1300)
                    self.assertIsNone(page.evaluate(src), "reconnected while the page was hidden")
                    # Restored from the back/forward cache: reconnect at once,
                    # even when other viewers keep the server's count above 0.
                    page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
                    page.wait_for_function(f"({src})() !== null", timeout=1000)
                finally:
                    context.close()

    def test_long_dog_name_stays_on_one_line(self):
        with patch.dict(os.environ, {"DOG_NAME": "Kotaro the Magnificent Shiba of Tokyo"}):
            for engine, browser in self.engines:
                with self.subTest(engine=engine):
                    context, page = self._page(browser, 320, 568)
                    try:
                        m = page.evaluate(MEASURE)
                        self.assertEqual(m["h1"]["lines"], 1, m["h1"])
                        self.assertGreaterEqual(m["h1"]["x"], 0)
                        self.assertLessEqual(m["h1"]["right"], 320)
                        self.assertEqual(m["overlaps"], [])
                    finally:
                        context.close()


if __name__ == "__main__":
    unittest.main()
