"""Submission evidence regressions with real Chromium and intercepted local pages."""

import time
import unittest
from unittest.mock import patch

from test_browser import fixture, script_namespace

try:
    from playwright.sync_api import TimeoutError as BrowserTimeout, sync_playwright
except ImportError:
    BrowserTimeout = None
    sync_playwright = None


NONCE = "CHECK_SUBMIT_FIXTURE_39218"
MODERN_ENDPOINT = "https://geminiweb-pa.clients6.google.com/v1/processSession"


@unittest.skipIf(sync_playwright is None, "optional Playwright package is not installed")
class BrowserSubmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "browser", None):
            cls.browser.close()
        if getattr(cls, "playwright", None):
            cls.playwright.stop()

    def setUp(self):
        self.namespace = script_namespace()
        self.context = self.browser.new_context(locale="en-US", service_workers="block")
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.posts = []

    def load(self, html, intercept=None):
        def respond(route):
            request = route.request
            if request.method == "POST":
                self.posts.append((request.url, request.post_data))
            if intercept is not None and intercept(route, request):
                return
            if request.method == "POST":
                route.fulfill(status=200, content_type="application/json", body='{"ok":true}',
                              headers={"Access-Control-Allow-Origin": "*"})
            elif request.method == "OPTIONS":
                route.fulfill(status=204, headers={"Access-Control-Allow-Origin": "*",
                              "Access-Control-Allow-Methods": "POST",
                              "Access-Control-Allow-Headers": "Content-Type"})
            elif request.is_navigation_request():
                route.fulfill(status=200, content_type="text/html", body=html)
            else:
                route.fulfill(status=200, content_type="text/plain", body="")
        self.context.route("**/*", respond)
        self.page.goto("https://gemini.google.com/app?hl=en", wait_until="networkidle")

    def probe(self, timeout=1.5):
        return self.namespace["test_browser_page"](self.page, timeout=timeout, nonce=NONCE)

    def counted_fixture(self, mode):
        return fixture(mode).replace("</script>", """
window.submitActions = {click: 0, enter: 0};
document.querySelector('button').addEventListener('click', () => window.submitActions.click++);
editor.addEventListener('keydown', event => {
  if (event.key === 'Enter') window.submitActions.enter++;
});
</script>""")

    def test_background_session_request_behind_overlay_does_not_prove_submission(self):
        overlay = '<div style="position:fixed;inset:0;z-index:999999;background:white">Loading</div>'
        html = fixture(initial=overlay).replace("</script>", """
window.submitActions = {click: 0, enter: 0};
document.querySelector('button').addEventListener('click', () => window.submitActions.click++);
editor.addEventListener('keydown', event => {
  if (event.key === 'Enter') window.submitActions.enter++;
});
editor.addEventListener('input', () => setTimeout(() => {
  fetch('https://geminiweb-pa.clients6.google.com/v1/processSession', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({initialization: 'not-the-prompt'})
  });
}, 80));
</script>""")
        self.load(html)
        started = time.monotonic()
        result = self.probe(timeout=1.2)
        elapsed = time.monotonic() - started
        self.assertEqual(self.posts, [(MODERN_ENDPOINT, '{"initialization":"not-the-prompt"}')])
        self.assertEqual(self.page.evaluate("window.submitActions"), {"click": 0, "enter": 0})
        self.assertFalse(result["submitted"], result)
        self.assertFalse(result["response_verified"], result)
        self.assertNotIn(result["status"], ("working", "region_blocked", "access_denied"), result)
        self.assertLess(elapsed, 2.0, "submission waits exceeded the shared time budget")

    def button_overlay(self, modal=False):
        initial = '<div id="button-cover" style="position:fixed;z-index:999999;background:white">Loading</div>'
        if modal:
            initial += '<div role="dialog" style="position:fixed;right:0;bottom:0;background:white">Review preferences</div>'
        return fixture("modern_success", initial=initial).replace("</script>", """
window.submitActions = {click: 0, enter: 0};
document.querySelector('button').addEventListener('click', () => window.submitActions.click++);
editor.addEventListener('keydown', event => {
  if (event.key === 'Enter') window.submitActions.enter++;
});
const buttonRect = document.querySelector('button').getBoundingClientRect();
Object.assign(document.querySelector('#button-cover').style, {
  left: buttonRect.left + 'px', top: buttonRect.top + 'px',
  width: buttonRect.width + 'px', height: buttonRect.height + 'px'
});
</script>""")

    def test_button_only_overlay_uses_one_enter_in_the_focused_open_editor(self):
        self.load(self.button_overlay())
        result = self.probe(timeout=2)
        self.assertEqual(result["status"], "working", result)
        self.assertTrue(result["response_verified"], result)
        self.assertTrue(result["submitted"], result)
        self.assertEqual(self.page.evaluate("window.submitActions"), {"click": 0, "enter": 1})
        self.assertEqual(len(self.posts), 1, self.posts)
        self.assertEqual(self.posts[0][0], MODERN_ENDPOINT)

    def test_visible_dialog_prevents_enter_behind_a_button_overlay(self):
        self.load(self.button_overlay(modal=True))
        result = self.probe(timeout=1.2)
        self.assertEqual(self.page.evaluate("window.submitActions"), {"click": 0, "enter": 0})
        self.assertFalse(self.posts, self.posts)
        self.assertFalse(result["submitted"], result)
        self.assertFalse(result["response_verified"], result)
        self.assertNotEqual(result["status"], "working", result)

    def test_focus_moved_to_another_input_prevents_enter_fallback(self):
        html = self.button_overlay().replace("</main>", '<input id="other-input" aria-label="Search"> </main>')
        html = html.replace("</script>", """
editor.addEventListener('input', () => setTimeout(() => document.querySelector('#other-input').focus(), 40));
window.otherEnterCount = 0;
document.querySelector('#other-input').addEventListener('keydown', event => {
  if (event.key === 'Enter') window.otherEnterCount++;
});
</script>""")
        self.load(html)
        result = self.probe(timeout=1.2)
        self.assertEqual(self.page.evaluate("window.submitActions"), {"click": 0, "enter": 0})
        self.assertEqual(self.page.evaluate("window.otherEnterCount"), 0)
        self.assertFalse(self.posts, self.posts)
        self.assertFalse(result["submitted"], result)
        self.assertFalse(result["response_verified"], result)
        self.assertNotEqual(result["status"], "working", result)

    def test_fresh_reply_is_verified_after_a_late_click_timeout_without_resending(self):
        self.load(self.counted_fixture("modern_success"))
        locator_type = type(self.page.locator("button"))
        actual_click = locator_type.click
        timeouts = []

        def late_timeout(locator, *args, **kwargs):
            actual_click(locator, *args, **kwargs)
            if kwargs.get("trial"):
                return
            self.page.wait_for_timeout(60)
            timeouts.append(True)
            raise BrowserTimeout("Locator.click: Timeout exceeded after an already dispatched click")

        with patch.object(locator_type, "click", late_timeout):
            result = self.probe(timeout=2.5)
        self.assertEqual(len(timeouts), 1, "the late-timeout fixture was not exercised exactly once")
        self.assertEqual(len(self.posts), 1, self.posts)
        self.assertEqual(self.page.evaluate("window.submitActions"), {"click": 1, "enter": 0})
        self.assertEqual(result["status"], "working", result)
        self.assertTrue(result["submitted"], result)
        self.assertTrue(result["response_verified"], result)

    def test_late_click_timeout_without_an_answer_never_resends_or_confirms_chat(self):
        self.load(self.counted_fixture("modern_no_answer"))
        locator_type = type(self.page.locator("button"))
        actual_click = locator_type.click
        timeouts = []

        def late_timeout(locator, *args, **kwargs):
            actual_click(locator, *args, **kwargs)
            if kwargs.get("trial"):
                return
            self.page.wait_for_timeout(60)
            timeouts.append(True)
            raise BrowserTimeout("Locator.click: Timeout exceeded after an already dispatched click")

        started = time.monotonic()
        with patch.object(locator_type, "click", late_timeout):
            result = self.probe(timeout=1.2)
        elapsed = time.monotonic() - started
        self.assertEqual(len(timeouts), 1)
        self.assertEqual(len(self.posts), 1, self.posts)
        self.assertEqual(self.page.evaluate("window.submitActions"), {"click": 1, "enter": 0})
        self.assertFalse(result["response_verified"], result)
        self.assertNotIn(result["status"], ("working", "region_blocked", "access_denied"), result)
        self.assertLess(elapsed, 2.0, "response waits exceeded the shared time budget")

    def test_late_background_response_cannot_replace_the_actual_request_status(self):
        html = self.button_overlay().replace("const mode = \"modern_success\";", 'const mode = "modern_no_answer";')
        html = html.replace("if (mode === 'no_answer'", """
  if (mode === 'modern_no_answer') {
    await fetch('/background-response-ready');
    return;
  }
  if (mode === 'no_answer'""")
        html = html.replace("</script>", """
editor.addEventListener('input', () => setTimeout(() => {
  fetch('https://geminiweb-pa.clients6.google.com/v1/processSession', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({background_initialization: true})
  });
}, 80));
editor.addEventListener('input', () => setTimeout(() => document.querySelector('#button-cover').remove(), 300));
</script>""")
        background = []
        statuses = []

        def intercept(route, request):
            if request.method == "POST" and "background_initialization" in (request.post_data or ""):
                background.append(route)
                return True
            if request.url.endswith("/background-response-ready"):
                self.assertEqual(len(background), 1, "the background request must start before submission")
                background[0].fulfill(status=403, content_type="application/json", body='{"error":"unrelated"}',
                                      headers={"Access-Control-Allow-Origin": "*"})
                route.fulfill(status=200, content_type="text/plain", body="ready")
                return True
            return False

        self.load(html, intercept=intercept)
        self.page.on("response", lambda response: statuses.append(
            ("background" if "background_initialization" in (response.request.post_data or "") else "actual", response.status))
            if response.url == MODERN_ENDPOINT else None)
        result = self.probe(timeout=1.6)
        self.assertEqual(statuses, [("actual", 200), ("background", 403)])
        self.assertEqual(len(self.posts), 2, self.posts)
        self.assertIn("background_initialization", self.posts[0][1])
        self.assertEqual(result["generation_http"], 200, result)
        self.assertNotEqual(result["status"], "access_denied", result)
        self.assertFalse(result["response_verified"], result)


if __name__ == "__main__":
    unittest.main()
