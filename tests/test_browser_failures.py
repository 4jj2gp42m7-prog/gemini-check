"""Browser UI failure regressions, using real Chromium and local routed pages only."""

import contextlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_browser import fixture, script_namespace

try:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright
except ImportError:
    PlaywrightError = None
    sync_playwright = None


NONCE = "CHECK_FAILURE_FIXTURE_741921"
SECRET = "DO_NOT_EXPORT_SESSION_TOKEN_44831"
PRIVATE_CHAT = "PRIVATE_EXISTING_CHAT_CONTENT_1182"


@unittest.skipIf(sync_playwright is None, "optional Playwright package is not installed")
class BrowserFailureTests(unittest.TestCase):
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
        self.logs = io.StringIO()

    def load(self, html, wait_until="networkidle"):
        def respond(route):
            request = route.request
            if request.method == "POST":
                self.posts.append(request.url)
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
        self.page.goto("https://gemini.google.com/app?hl=en", wait_until=wait_until)

    def probe(self, timeout=1.2):
        with contextlib.redirect_stdout(self.logs), contextlib.redirect_stderr(self.logs):
            return self.namespace["test_browser_page"](self.page, timeout=timeout, nonce=NONCE)

    def assert_safe(self, result):
        encoded = json.dumps(result, ensure_ascii=False) + self.logs.getvalue()
        self.assertNotIn(SECRET, encoded)
        self.assertNotIn(PRIVATE_CHAT, encoded)
        self.assertNotIn("?session_token=", encoded)
        self.assertFalse(result.get("response_verified"), result)
        self.assertNotEqual(result["status"], "working", result)

    def test_closed_page_reports_its_stage_instead_of_generic_unknown(self):
        self.load(fixture())
        self.page.close()
        result = self.probe(timeout=.4)
        self.assertEqual(result["status"], "browser_closed", result)
        self.assertEqual(result.get("stage"), "detect_gate", result)
        self.assertEqual(result.get("error", {}).get("kind"), "page_closed", result)
        self.assertEqual(result.get("error", {}).get("reason"), "page_closed", result)
        self.assertFalse(result["submitted"])
        self.assert_safe(result)

    def test_crashed_renderer_is_distinct_from_a_closed_page_or_region_ban(self):
        self.load(fixture(initial="<p>%s %s</p>" % (PRIVATE_CHAT, SECRET)))
        with self.page.expect_event("crash", timeout=3000):
            with self.assertRaises(PlaywrightError):
                self.page.goto("chrome://crash", timeout=3000)
        self.assertFalse(self.page.is_closed(), "renderer crash should leave the page target open")
        result = self.probe(timeout=.4)
        self.assertEqual(result["status"], "browser_crashed", result)
        self.assertEqual(result.get("stage"), "detect_gate", result)
        self.assertEqual(result.get("error", {}).get("kind"), "page_crashed", result)
        self.assertEqual(result.get("error", {}).get("reason"), "page_crashed", result)
        self.assertFalse(result["submitted"], result)
        args = SimpleNamespace(browser_profile=None, proxy="", browser_timeout=1, headed=False)
        with patch.dict(self.namespace, {"probe_browser": lambda **kwargs: dict(result)}):
            route = self.namespace["check_browser_route"](args)
        self.assertEqual(route["assessment"]["status"], "inconclusive", route)
        with contextlib.redirect_stdout(self.logs):
            self.namespace["print_report"]({"checked_at": "2026-11-07T00:00:00+00:00",
                                            "results": [route]})
        self.assertIn("page_crashed", self.logs.getvalue())
        self.assert_safe(result)

    def test_overlay_reports_submit_timeout_without_sending_behind_it(self):
        overlay = '<div style="position:fixed;inset:0;z-index:999999;background:white">Loading</div>'
        self.load(fixture(initial=overlay))
        result = self.probe(timeout=1)
        self.assertEqual(result["status"], "timeout", result)
        self.assertEqual(result.get("stage"), "submit", result)
        self.assertEqual(result.get("error", {}).get("kind"), "timeout", result)
        self.assertEqual(result.get("error", {}).get("reason"), "blocked_by_overlay", result)
        self.assertFalse(result["submitted"], result)
        self.assertFalse(self.posts, self.posts)
        args = SimpleNamespace(browser_profile=None, proxy="", browser_timeout=1, headed=False)
        with patch.dict(self.namespace, {"probe_browser": lambda **kwargs: dict(result)}):
            route = self.namespace["check_browser_route"](args)
        self.assertEqual(route["assessment"]["status"], "inconclusive", route)
        with contextlib.redirect_stdout(self.logs):
            self.namespace["print_report"]({"checked_at": "2026-11-07T00:00:00+00:00",
                                            "results": [route]})
        for text in ("Этап", "отправка сообщения", "blocked_by_overlay", "TimeoutError"):
            self.assertIn(text, self.logs.getvalue())
        self.assert_safe(result)

    def test_transient_overlay_can_clear_within_the_overall_deadline(self):
        overlay = '<div id="loading" style="position:fixed;inset:0;z-index:999999;background:white">Loading</div>'
        html = fixture(initial=overlay).replace("</script>", """
setTimeout(() => document.getElementById('loading').remove(), 5500);
</script>""")
        self.load(html, wait_until="domcontentloaded")
        result = self.probe(timeout=8)
        self.assertEqual(result["status"], "working", result)
        self.assertTrue(result["submitted"])
        self.assertTrue(result["response_verified"])
        self.assertEqual(result.get("stage"), "complete", result)
        self.assertFalse(result.get("error"), result)

    def test_noneditable_playwright_error_has_a_controlled_reason(self):
        self.load(fixture(initial="<p>%s</p>" % PRIVATE_CHAT))
        message = "Element is not editable: session_token=%s" % SECRET
        def broken_editor(page):
            raise PlaywrightError(message)
        with patch.dict(self.namespace, {"_browser_composer": broken_editor}):
            result = self.probe()
        self.assertEqual(result.get("stage"), "locate_editor", result)
        self.assertEqual(result.get("error", {}).get("kind"), "playwright_error", result)
        self.assertEqual(result.get("error", {}).get("reason"), "element_not_editable", result)
        self.assertEqual(result.get("error", {}).get("type"), "Error", result)
        self.assert_safe(result)

    def test_unexpected_error_never_exports_exception_or_page_text(self):
        self.load(fixture(initial="<p>%s</p>" % PRIVATE_CHAT))
        def broken_gate(page):
            raise RuntimeError("private page https://gemini.google.com/app/private-chat?session_token=" + SECRET)
        with patch.dict(self.namespace, {"_browser_gate": broken_gate}):
            result = self.probe()
        self.assertEqual(result.get("stage"), "detect_gate", result)
        self.assertEqual(result.get("error", {}).get("kind"), "unexpected_error", result)
        self.assertEqual(result.get("error", {}).get("reason"), "unexpected_error", result)
        self.assertEqual(result.get("error", {}).get("type"), "RuntimeError", result)
        self.assertIsInstance(result.get("diagnostics"), dict, result)
        self.assert_safe(result)

    def test_answer_read_failure_preserves_observed_submission(self):
        self.load(fixture())
        actual = self.namespace["_browser_assistants"]
        calls = []
        def fail_after_submit(page):
            calls.append(True)
            if len(calls) > 1:
                page.wait_for_timeout(60)
                raise RuntimeError("response contained " + SECRET)
            return actual(page)
        with patch.dict(self.namespace, {"_browser_assistants": fail_after_submit}):
            result = self.probe(timeout=2)
        self.assertTrue(self.posts, "the local fixture did not observe a generation request")
        self.assertTrue(result["submitted"], result)
        self.assertTrue(result["request_seen"], result)
        self.assertEqual(result.get("stage"), "wait_answer", result)
        self.assertEqual(result.get("error", {}).get("kind"), "unexpected_error", result)
        self.assert_safe(result)

    def test_submit_failure_preserves_generation_request_already_seen(self):
        self.load(fixture("no_answer"))
        locator_type = type(self.page.locator("button"))
        actual_click = locator_type.click
        def fail_after_click(locator, *args, **kwargs):
            actual_click(locator, *args, **kwargs)
            if kwargs.get("trial"):
                return
            self.page.wait_for_timeout(60)
            raise RuntimeError("private submit state " + SECRET)
        with patch.object(locator_type, "click", fail_after_click):
            result = self.probe(timeout=2)
        self.assertTrue(self.posts, "the local fixture did not observe a generation request")
        self.assertTrue(result["submitted"], result)
        self.assertTrue(result["request_seen"], result)
        self.assertEqual(result.get("stage"), "submit", result)
        self.assertEqual(result.get("error", {}).get("kind"), "unexpected_error", result)
        self.assert_safe(result)

    def test_delayed_editable_composer_can_finish_normally(self):
        html = fixture().replace('contenteditable="true"', 'contenteditable="false"')
        html = html.replace("</script>", """
setTimeout(() => editor.setAttribute('contenteditable', 'true'), 350);
</script>""")
        self.load(html, wait_until="domcontentloaded")
        result = self.probe(timeout=2.5)
        self.assertEqual(result["status"], "working", result)
        self.assertTrue(result["submitted"])
        self.assertTrue(result["response_verified"])
        self.assertEqual(result.get("stage"), "complete", result)
        self.assertFalse(result.get("error"), result)

    def test_replaced_composer_during_hydration_can_finish_normally(self):
        html = fixture().replace("const editor =", "let editor =")
        html = html.replace('contenteditable="true"', 'contenteditable="false"')
        html = html.replace("</script>", """
setTimeout(() => {
  const replacement = editor.cloneNode(true);
  replacement.setAttribute('contenteditable', 'true');
  editor.replaceWith(replacement);
  editor = replacement;
}, 350);
</script>""")
        self.load(html, wait_until="domcontentloaded")
        result = self.probe(timeout=2.5)
        self.assertEqual(result["status"], "working", result)
        self.assertTrue(result["response_verified"])
        self.assertEqual(result.get("stage"), "complete", result)
        self.assertFalse(result.get("error"), result)

    def test_readonly_composer_does_not_become_a_region_ban(self):
        html = fixture().replace('contenteditable="true"', 'contenteditable="false"')
        self.load(html)
        result = self.probe(timeout=.6)
        self.assertFalse(result["submitted"], result)
        self.assertEqual(result.get("stage"), "locate_editor", result)
        self.assertEqual(result["status"], "unknown", result)
        self.assert_safe(result)


if __name__ == "__main__":
    unittest.main()
