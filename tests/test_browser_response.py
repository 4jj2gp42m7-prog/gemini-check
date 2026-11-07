"""Response diagnostics, with real Chromium and locally intercepted pages only."""

import contextlib
import io
import json
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_browser import fixture, script_namespace

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


NONCE = "CHECK_RESPONSE_FIXTURE_18429"
SECRET = "PRIVATE_EXISTING_CHAT_SECRET_827492"


@unittest.skipIf(sync_playwright is None, "optional Playwright package is not installed")
class BrowserResponseTests(unittest.TestCase):
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
        self.gets = []
        self.pending = []
        self.output = io.StringIO()

    def load(self, html, post_action="finish", post_body='{"ok":true}',
             post_content_type="application/json", get_body=None):
        def respond(route):
            request = route.request
            if request.method == "POST":
                self.posts.append(request.url)
                if post_action == "fail":
                    route.abort("failed")
                elif post_action == "pending":
                    self.pending.append(route)
                else:
                    route.fulfill(status=200, content_type=post_content_type, body=post_body,
                                  headers={"Access-Control-Allow-Origin": "*"})
            elif request.method == "GET" and request.url.startswith("https://geminiweb-pa.clients6.google.com/v1/processSession"):
                self.gets.append(request.url)
                route.fulfill(status=200, content_type="application/json", body=get_body or '{"ok":true}',
                              headers={"Access-Control-Allow-Origin": "*"})
            elif request.method == "OPTIONS":
                route.fulfill(status=204, headers={"Access-Control-Allow-Origin": "*",
                              "Access-Control-Allow-Methods": "POST",
                              "Access-Control-Allow-Headers": "Content-Type"})
            elif request.is_navigation_request():
                route.fulfill(status=200, content_type="text/html", body=html)
            else:
                route.fulfill(status=200, content_type="text/plain", body="")

        def abort_owned_routes():
            for route in self.pending:
                try:
                    route.abort()
                except Exception:
                    pass

        self.addCleanup(abort_owned_routes)
        self.context.route("**/*", respond)
        self.page.goto("https://gemini.google.com/app?hl=en", wait_until="networkidle")

    def probe(self, **kwargs):
        with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output):
            return self.namespace["test_browser_page"](self.page, timeout=1.0, nonce=NONCE, **kwargs)

    def assert_inconclusive(self, result):
        self.assertNotIn(result["status"], ("working", "region_blocked", "access_denied"), result)
        self.assertFalse(result["response_verified"], result)

    def test_completed_backend_request_without_answer_is_not_working_or_private_output(self):
        old = "<model-response><message-content>%s</message-content></model-response>" % SECRET
        self.load(fixture("modern_no_answer", initial=old))
        result = self.probe()
        self.assertEqual(result["status"], "timeout", result)
        self.assertEqual(result.get("backend_requests"), {"total": 1, "finished": 1, "failed": 0, "pending": 0}, result)
        self.assertNotIn("lines", result.get("page_info", {}), result)
        self.assert_inconclusive(result)
        args = SimpleNamespace(browser_profile=None, proxy="", browser_timeout=1, headed=False)
        with patch.dict(self.namespace, {"probe_browser": lambda **kwargs: dict(result)}):
            route = self.namespace["check_browser_route"](args)
        with contextlib.redirect_stdout(self.output):
            self.namespace["print_report"]({"checked_at": "2026-11-07T00:00:00+00:00", "results": [route]})
        self.assertNotIn(SECRET, json.dumps(result) + self.output.getvalue())

    def test_failed_backend_request_is_counted_without_inventing_an_ip_ban(self):
        self.load(fixture("modern_no_answer"), post_action="fail")
        result = self.probe()
        self.assertEqual(result.get("backend_requests"), {"total": 1, "finished": 0, "failed": 1, "pending": 0}, result)
        self.assert_inconclusive(result)

    def test_pending_backend_request_is_counted_without_reading_its_body(self):
        self.load(fixture("modern_no_answer"), post_action="pending")
        result = self.probe()
        self.assertEqual(len(self.pending), 1)
        self.assertEqual(result.get("backend_requests"), {"total": 1, "finished": 0, "failed": 0, "pending": 1}, result)
        self.assert_inconclusive(result)

    def test_new_google_error_alert_after_submit_is_reported_without_raw_text(self):
        html = fixture("modern_no_answer").replace("if (mode === 'no_answer'", """
if (mode === 'modern_no_answer') {
  const error = document.createElement('div');
  error.setAttribute('role', 'alert');
  error.textContent = 'Something went wrong. Please try again. PRIVATE_EXISTING_CHAT_SECRET_827492';
  document.body.append(error);
  return;
}
if (mode === 'no_answer'""", 1)
        self.load(html)
        result = self.probe()
        self.assertEqual(result["status"], "site_error", result)
        self.assertTrue(result["submitted"], result)
        self.assert_inconclusive(result)
        self.assertNotIn(SECRET, json.dumps(result) + self.output.getvalue())

    def test_existing_error_alert_does_not_hide_a_new_verified_answer(self):
        old = '<div role="alert">Something went wrong. Please try again.</div>'
        self.load(fixture("modern_success", initial=old))
        result = self.probe()
        self.assertEqual(result["status"], "working", result)
        self.assertTrue(result["response_verified"], result)

    def test_opt_in_visible_page_info_can_show_an_unrecognized_reply_without_confirming_it(self):
        html = fixture("modern_success").replace("document.createElement('model-response')", "document.createElement('article')")
        html = html.replace("content.className = 'model-response-text';", "content.className = 'alternate-response-layout';")
        self.load(html)
        result = self.probe(page_info=True)
        self.assert_inconclusive(result)
        info = result.get("page_info", {})
        lines = info.get("lines", [])
        self.assertIn(NONCE, "\n".join(lines), result)
        self.assertEqual(info.get("known_assistant_count"), 0, result)
        self.assertFalse(info.get("known_nonce_found"), result)
        self.assertTrue(info.get("user_query_seen"), result)

    def test_completed_http_200_google_error_preserves_its_real_status(self):
        error = {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "Request refused " + SECRET}}
        self.load(fixture("modern_no_answer"), post_body=json.dumps(error))
        result = self.probe()
        self.assertEqual(result["status"], "access_denied", result)
        self.assertFalse(result["response_verified"], result)
        self.assertEqual(result.get("response_error", {}).get("code"), 403, result)
        self.assertEqual(result["response_error"]["status"], "PERMISSION_DENIED", result)
        self.assertNotIn(SECRET, json.dumps(result) + self.output.getvalue())

    def test_webchannel_acknowledgement_number_is_not_a_permission_error(self):
        self.load(fixture("modern_no_answer"), post_body="8\n[0,0,7]\n", post_content_type="text/plain")
        result = self.probe()
        self.assertEqual(result["status"], "timeout", result)
        self.assertFalse(result["response_verified"], result)
        self.assertNotIn("response_error", result)
        self.assertFalse(result.get("rpc_errors"), result)

    def test_verified_model_reply_wins_over_an_unrelated_rpc_error(self):
        error = {"error": {"code": 7, "status": "PERMISSION_DENIED", "message": SECRET}}
        self.load(fixture("modern_success"), post_body=json.dumps(error))
        result = self.probe()
        self.assertEqual(result["status"], "working", result)
        self.assertTrue(result["response_verified"], result)
        self.assertTrue(result["submitted"], result)
        self.assertNotIn("response_error", result)
        self.assertNotIn(SECRET, json.dumps(result) + self.output.getvalue())

    def test_gemini_ui_error_13_is_not_mislabelled_as_grpc_internal(self):
        html = fixture("modern_no_answer").replace("if (mode === 'no_answer'", """
if (mode === 'modern_no_answer') {
  const error = document.createElement('div');
  error.setAttribute('role', 'alert');
  error.textContent = 'Something went wrong (13). Please try again. PRIVATE_EXISTING_CHAT_SECRET_827492';
  document.body.append(error);
  return;
}
if (mode === 'no_answer'""", 1)
        self.load(html)
        result = self.probe()
        self.assertEqual(result["status"], "site_error", result)
        self.assertFalse(result["response_verified"], result)
        self.assertEqual(result.get("response_error", {}).get("source"), "gemini_ui", result)
        self.assertEqual(result["response_error"]["code"], 13, result)
        self.assertNotEqual(result["response_error"]["status"], "INTERNAL", result)
        self.assertNotIn(SECRET, json.dumps(result) + self.output.getvalue())

    def test_completed_backend_get_error_is_observed_without_requiring_a_post_body(self):
        html = fixture("modern_no_answer").replace("if (mode === 'no_answer'", """
if (mode === 'modern_no_answer') {
  await fetch('https://geminiweb-pa.clients6.google.com/v1/processSession?RID=response-fixture', {method: 'GET'});
  return;
}
if (mode === 'no_answer'""", 1)
        error = {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": SECRET}}
        self.load(html, post_body="8\n[0,0,7]\n", post_content_type="text/plain", get_body=json.dumps(error))
        result = self.probe()
        self.assertEqual(len(self.posts), 1, self.posts)
        self.assertEqual(len(self.gets), 1, self.gets)
        self.assertEqual(result["status"], "access_denied", result)
        self.assertEqual(result.get("response_error", {}).get("code"), 403, result)
        self.assertFalse(result["response_verified"], result)
        self.assertNotIn(SECRET, json.dumps(result) + self.output.getvalue())


class BrowserResponseCliTests(unittest.TestCase):
    def setUp(self):
        self.namespace = script_namespace()

    def arguments(self, *options):
        with patch.object(sys, "argv", ["gemini-check", *options]):
            return self.namespace["arguments"]()

    def test_page_info_option_is_available_for_an_anonymous_browser(self):
        args, key = self.arguments("--browser", "--browser-page-info", "--json")
        self.assertTrue(getattr(args, "browser_page_info", getattr(args, "page_info", False)))
        self.assertFalse(args.browser_profile)
        self.assertEqual(key, "")

    def test_page_info_requires_browser_and_cannot_expose_a_persistent_profile(self):
        with tempfile.TemporaryDirectory(prefix="gemini-response-profile-") as profile:
            for options in (("--browser-page-info", "--json"),
                            ("--browser", "--browser-page-info", "--browser-profile", profile, "--json")):
                with self.subTest(options=options):
                    with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                        self.arguments(*options)
                    self.assertEqual(raised.exception.code, 64)


if __name__ == "__main__":
    unittest.main()
