"""Real Chromium regression tests against local, intercepted Gemini-shaped pages.

These fixtures never contact Google. Install the optional browser runtime, then run
its Python with ``-m unittest discover -s tests -p test_browser.py -v``.
"""

import contextlib
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "gemini-check.sh"
NONCE = "CHECK_LOCAL_FIXTURE_381942"


def script_namespace():
    source = SCRIPT.read_text(encoding="utf-8")
    source = source.split("<<'PYTHON_GEMINI_CHECK'\n", 1)[1].rsplit("\nPYTHON_GEMINI_CHECK", 1)[0]
    namespace = {"__name__": "gemini_browser_test", "__file__": str(SCRIPT)}
    exec(compile(source, str(SCRIPT) + ":python", "exec"), namespace)
    return namespace


def fixture(mode="success", initial=""):
    """Render a composer, then independently emulate request and UI updates."""
    return """<!doctype html>
<html><head><title>Gemini</title></head><body>
<main>
%s
<section id="conversation"></section>
<a href="https://accounts.google.com/ServiceLogin">Sign in</a>
<rich-textarea>
  <div role="textbox" class="ql-editor" aria-label="Enter a prompt here"
       contenteditable="true" data-placeholder="Ask Gemini"></div>
</rich-textarea>
<button type="button" class="send-button" aria-label="Send message"
        data-test-id="send-button">Send</button>
</main>
<script>
const mode = %s;
let sent = false;
const editor = document.querySelector('[contenteditable]');
const conversation = document.querySelector('#conversation');
function assistant(text, hidden=false) {
  const node = document.createElement('model-response');
  const content = document.createElement('message-content');
  content.className = 'model-response-text';
  content.textContent = text;
  node.append(content);
  if (hidden) node.style.display = 'none';
  conversation.append(node);
}
async function send() {
  if (sent) return;
  sent = true;
  const prompt = editor.innerText;
  const match = prompt.match(/CHECK_[A-Za-z0-9_]+/);
  const marker = match ? match[0] : 'NO_MARKER';
  const user = document.createElement('user-query');
  user.className = 'user-query-content';
  user.textContent = prompt;
  conversation.append(user);
  editor.textContent = '';
  if (mode !== 'no_request') {
    const endpoints = {
      other_host_request: 'https://telemetry.example.invalid/collect',
      modern_success: 'https://geminiweb-pa.clients6.google.com/v1/processSession',
      modern_no_answer: 'https://geminiweb-pa.clients6.google.com/v1/processSession',
      modern_other_host: 'https://unrelated-pa.clients6.google.com/v1/processSession',
      modern_other_path: 'https://geminiweb-pa.clients6.google.com/v1/collectTelemetry'
    };
    const url = endpoints[mode] || '/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate';
    const body = mode.startsWith('modern_') ? JSON.stringify({serialized_session: 'encoded-fixture-payload'}) : JSON.stringify({prompt: mode === 'wrong_request' ? 'unrelated telemetry' : prompt});
    await fetch(url, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body
    });
  }
  if (mode === 'no_answer' || mode === 'user_echo' || mode === 'old_only' || mode === 'modern_no_answer') return;
  if (mode === 'after_region') {
    document.querySelector('main').textContent = "Gemini isn't available in your country";
    return;
  }
  if (mode === 'after_challenge') {
    document.querySelector('main').textContent = 'Our systems have detected unusual traffic from your computer network';
    return;
  }
  if (mode === 'after_quota') {
    document.querySelector('main').textContent = "You've reached your limit for Gemini. Try again later.";
    return;
  }
  if (mode === 'after_login') {
    const dialog = document.createElement('div');
    dialog.setAttribute('role', 'dialog');
    dialog.innerHTML = '<h2>Sign in to continue</h2><p>To continue this chat, sign in with your Google Account.</p><a href="https://accounts.google.com/ServiceLogin">Sign in</a>';
    document.body.append(dialog);
    return;
  }
  if (mode === 'reuse') {
    document.querySelector('model-response message-content').textContent = marker;
    return;
  }
  if (mode === 'echo_assistant') {
    assistant(prompt);
    return;
  }
  if (mode === 'unmatched_reply') {
    assistant('This is an unrelated answer.');
    return;
  }
  assistant(marker, mode === 'hidden_reply');
}
document.querySelector('button').addEventListener('click', send);
editor.addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    send();
  }
});
</script></body></html>""" % (initial, json.dumps(mode))


@unittest.skipIf(sync_playwright is None, "optional Playwright package is not installed")
class BrowserPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.namespace = script_namespace()
        if "test_browser_page" not in cls.namespace:
            raise AssertionError("the script does not contain the browser test helper")
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "browser", None):
            cls.browser.close()
        if getattr(cls, "playwright", None):
            cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(locale="en-US", service_workers="block")
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.requests = []

    def load(self, html, url="https://gemini.google.com/app?hl=en", post_status=200):
        def handler(route):
            request = route.request
            self.requests.append((request.method, request.url, request.post_data))
            if request.method == "OPTIONS":
                route.fulfill(status=204, headers={"Access-Control-Allow-Origin": "*",
                              "Access-Control-Allow-Methods": "POST", "Access-Control-Allow-Headers": "Content-Type"})
            elif request.method == "POST":
                route.fulfill(status=post_status, content_type="application/json", body='{"ok":true}',
                              headers={"Access-Control-Allow-Origin": "*"})
            elif request.is_navigation_request():
                route.fulfill(status=200, content_type="text/html", body=html)
            else:
                route.fulfill(status=200, content_type="text/plain", body="")
        self.context.route("**/*", handler)
        self.page.goto(url, wait_until="networkidle")

    def probe(self, timeout=1.2):
        return self.namespace["test_browser_page"](self.page, timeout=timeout, nonce=NONCE)

    def assert_not_working(self, result):
        self.assertNotEqual(result["status"], "working", result)
        self.assertFalse(result.get("response_verified"), result)

    def test_new_model_answer_confirms_web(self):
        self.load(fixture())
        result = self.probe()
        self.assertEqual(result["status"], "working", result)
        self.assertTrue(result["submitted"])
        self.assertTrue(result["response_verified"])
        self.assertTrue(any(method == "POST" and NONCE in (body or "") for method, _, body in self.requests))

    def test_sign_in_link_does_not_prevent_anonymous_chat(self):
        self.load(fixture())
        self.assertTrue(self.page.get_by_text("Sign in", exact=True).is_visible())
        self.assertEqual(self.probe()["status"], "working")

    def test_current_web_backend_accepts_serialized_payload_without_literal_nonce(self):
        self.load(fixture("modern_success"))
        result = self.probe()
        self.assertEqual(result["status"], "working", result)
        self.assertTrue(result["response_verified"])
        calls = [(url, body) for method, url, body in self.requests if method == "POST"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "https://geminiweb-pa.clients6.google.com/v1/processSession")
        self.assertNotIn(NONCE, calls[0][1])

    def test_current_web_backend_request_alone_does_not_confirm_chat(self):
        self.load(fixture("modern_no_answer"))
        result = self.probe()
        self.assertTrue(result["submitted"])
        self.assert_not_working(result)

    def test_neighboring_clients6_host_or_path_is_not_the_web_backend(self):
        for mode in ("modern_other_host", "modern_other_path"):
            with self.subTest(mode=mode):
                self.load(fixture(mode))
                self.assert_not_working(self.probe())

    def test_user_prompt_echo_is_not_a_model_answer(self):
        self.load(fixture("user_echo"))
        result = self.probe()
        self.assertTrue(result["submitted"])
        self.assert_not_working(result)

    def test_existing_answer_with_marker_is_not_a_new_answer(self):
        previous = "<model-response><message-content>%s</message-content></model-response>" % NONCE
        self.load(fixture("old_only", previous))
        self.assert_not_working(self.probe())

    def test_existing_unrelated_answer_does_not_confirm_new_prompt(self):
        previous = "<model-response><message-content>An old response</message-content></model-response>"
        self.load(fixture("old_only", previous))
        self.assert_not_working(self.probe())

    def test_prompt_echo_in_model_container_is_not_a_reply(self):
        self.load(fixture("echo_assistant"))
        self.assert_not_working(self.probe())

    def test_new_dom_marker_without_network_request_is_not_success(self):
        self.load(fixture("no_request"))
        self.assert_not_working(self.probe())
        self.assertFalse(any(method == "POST" for method, _, _ in self.requests))

    def test_unrelated_post_does_not_confirm_our_probe(self):
        self.load(fixture("wrong_request"))
        self.assert_not_working(self.probe())

    def test_post_to_another_host_does_not_confirm_gemini(self):
        self.load(fixture("other_host_request"))
        self.assert_not_working(self.probe())
        self.assertTrue(any(method == "POST" and "telemetry.example.invalid" in url for method, url, _ in self.requests))

    def test_hidden_model_answer_is_not_success(self):
        self.load(fixture("hidden_reply"))
        self.assert_not_working(self.probe())

    def test_unrelated_new_answer_does_not_confirm_our_probe(self):
        self.load(fixture("unmatched_reply"))
        self.assert_not_working(self.probe())

    def test_report_does_not_export_previous_chat_text(self):
        private = "PRIVATE_OLD_CHAT_FIXTURE_492837"
        previous = "<model-response><message-content>%s</message-content></model-response>" % private
        self.load(fixture(initial=previous))
        result = self.probe()
        self.assertEqual(result["status"], "working", result)
        self.assertNotIn(private, json.dumps(result))

    def test_reused_model_node_with_fresh_answer_is_accepted(self):
        previous = "<model-response><message-content>An old response</message-content></model-response>"
        self.load(fixture("reuse", previous))
        self.assertEqual(self.probe()["status"], "working")

    def test_no_answer_times_out_without_inventing_an_ip_ban(self):
        self.load(fixture("no_answer"))
        result = self.probe()
        self.assertEqual(result["status"], "timeout", result)
        self.assert_not_working(result)

    def test_visible_region_refusal_is_explicit(self):
        self.load(fixture(initial="<div>Gemini isn't available in your country</div>"))
        result = self.probe()
        self.assertEqual(result["status"], "region_blocked", result)
        self.assertFalse(result["submitted"])

    def test_hidden_region_templates_do_not_block_a_working_chat(self):
        initial = """<div hidden>Gemini isn't available in your country</div>
<div style="display:none">Gemini isn't available in your country</div>
<script>const errorText="Gemini isn't available in your country";</script>"""
        self.load(fixture(initial=initial))
        self.assertEqual(self.probe()["status"], "working")

    def test_region_refusal_after_submit_is_detected(self):
        self.load(fixture("after_region"))
        result = self.probe()
        self.assertEqual(result["status"], "region_blocked", result)
        self.assertTrue(result["submitted"])

    def test_interactive_probe_can_resume_after_manual_login(self):
        html = fixture().replace("if (sent) return;", """if (sent) return;
  if (!window.fixtureSignedIn) {
    const dialog = document.createElement('div');
    dialog.setAttribute('role', 'dialog');
    dialog.textContent = 'To continue this chat, sign in with your Google Account.';
    document.body.append(dialog);
    setTimeout(() => { window.fixtureSignedIn = true; dialog.remove(); }, 400);
    return;
  }""")
        self.load(html)
        result = self.namespace["test_browser_page"](self.page, timeout=3, nonce=NONCE, interactive=True)
        self.assertEqual(result["status"], "working", result)
        self.assertTrue(result["response_verified"])
        self.assertNotEqual(result["nonce"], NONCE)

    def test_mandatory_sign_in_dialog_after_submit_requires_login(self):
        self.load(fixture("after_login"))
        result = self.probe()
        self.assertEqual(result["status"], "login_required", result)
        self.assertTrue(result["submitted"])
        self.assert_not_working(result)

    def test_challenge_after_submit_is_not_region_ban(self):
        self.load(fixture("after_challenge"))
        result = self.probe()
        self.assertEqual(result["status"], "challenge", result)
        self.assert_not_working(result)

    def test_quota_after_submit_is_not_region_ban(self):
        self.load(fixture("after_quota"))
        result = self.probe()
        self.assertEqual(result["status"], "quota", result)
        self.assert_not_working(result)

    def test_generation_http_refusals_are_not_masked_by_dom_text(self):
        for status, expected in ((403, "access_denied"), (429, "quota"), (503, "network_error")):
            with self.subTest(status=status):
                context = self.browser.new_context(locale="en-US", service_workers="block")
                try:
                    page = context.new_page()
                    def handler(route):
                        if route.request.method == "POST":
                            route.fulfill(status=status, content_type="application/json", body='{"error":"fixture"}')
                        else:
                            route.fulfill(status=200, content_type="text/html", body=fixture())
                    context.route("**/*", handler)
                    page.goto("https://gemini.google.com/app", wait_until="networkidle")
                    result = self.namespace["test_browser_page"](page, timeout=1.2, nonce=NONCE)
                    self.assertEqual(result["status"], expected, result)
                    self.assert_not_working(result)
                finally:
                    context.close()

    def test_account_redirect_requires_login(self):
        self.load("<html><body>Sign in with your Google Account</body></html>",
                  "https://accounts.google.com/ServiceLogin?service=bard")
        result = self.probe()
        self.assertEqual(result["status"], "login_required", result)
        self.assertFalse(result["submitted"])

    def test_captcha_page_is_not_region_ban(self):
        self.load("<html><body>Our systems have detected unusual traffic from your computer network</body></html>",
                  "https://www.google.com/sorry/index")
        result = self.probe()
        self.assertEqual(result["status"], "challenge", result)
        self.assertFalse(result["submitted"])

    def test_unrecognized_layout_is_unknown(self):
        self.load("<html><body><h1>Gemini</h1><div>Maintenance</div></body></html>")
        result = self.probe()
        self.assertEqual(result["status"], "unknown", result)
        self.assertFalse(result["submitted"])

    def test_authenticated_socks_proxy_is_rejected_explicitly(self):
        result = self.namespace["probe_browser"](proxy="socks5h://test:private@127.0.0.1:1080", timeout=1)
        self.assertEqual(result["status"], "proxy_unsupported", result)
        self.assert_not_working(result)

    def test_full_probe_uses_browser_context_for_trace_and_chat(self):
        contexts, launches, navigations = [], [], []
        real_browser = self.browser
        html = fixture()

        class Browser:
            def new_context(self, **kwargs):
                context = real_browser.new_context(**kwargs)
                contexts.append(context)
                def handler(route):
                    url = route.request.url
                    navigations.append((id(context), url))
                    if "cloudflare.com/cdn-cgi/trace" in url:
                        route.fulfill(status=200, content_type="text/plain",
                                      body="ip=203.0.113.73\nloc=DE\ncolo=FRA\nwarp=off\n")
                    elif route.request.method == "POST":
                        route.fulfill(status=200, content_type="application/json", body='{"ok":true}')
                    else:
                        route.fulfill(status=200, content_type="text/html", body=html)
                context.route("**/*", handler)
                return context

            def close(self):
                pass  # The real test browser belongs to setUpClass.

        class Driver:
            def __enter__(self):
                def launch(**kwargs):
                    launches.append(kwargs)
                    return Browser()
                return SimpleNamespace(chromium=SimpleNamespace(launch=launch))

            def __exit__(self, *args):
                return False

        with patch("playwright.sync_api.sync_playwright", return_value=Driver()), \
                patch.dict(os.environ, {"GEMINI_KEY": "private-key-not-for-browser",
                                        "HTTPS_PROXY": "http://unexpected.invalid:8080"}):
            result = self.namespace["probe_browser"](timeout=5)
        self.assertEqual(result["status"], "working", result)
        self.assertEqual(result["ip"], "203.0.113.73")
        self.assertEqual(result["country"], "DE")
        self.assertEqual(len(contexts), 1)
        self.assertTrue(any("cloudflare.com/cdn-cgi/trace" in url for _, url in navigations))
        self.assertTrue(any("gemini.google.com" in url for _, url in navigations))
        self.assertEqual(len({identity for identity, _ in navigations}), 1)
        self.assertNotIn("GEMINI_KEY", launches[0]["env"])
        self.assertNotIn("HTTPS_PROXY", launches[0]["env"])
        self.assertIn("--no-proxy-server", launches[0]["args"])


class BrowserCliTests(unittest.TestCase):
    def setUp(self):
        self.namespace = script_namespace()

    def arguments(self, *args):
        with patch.object(sys, "argv", ["gemini-check", *args]):
            return self.namespace["arguments"]()

    def test_browser_mode_never_uses_or_validates_an_api_key(self):
        with patch.dict(os.environ, {"GEMINI_KEY": "not a valid\nAPI key"}):
            args, key = self.arguments("--browser", "--json")
        self.assertTrue(args.browser)
        self.assertEqual(key, "")

    def test_browser_mode_rejects_curl_family_labels(self):
        for family in ("-4", "-6", "--both"):
            with self.subTest(family=family):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                    self.arguments("--browser", family, "--json")
                self.assertEqual(raised.exception.code, 64)

    def test_browser_only_options_require_browser_mode(self):
        for options in (("--headed",), ("--browser-profile", "/tmp/gemini-test-profile")):
            with self.subTest(options=options):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                    self.arguments(*options, "--json")
                self.assertEqual(raised.exception.code, 64)

    def test_browser_main_does_not_run_curl_or_api_probes(self):
        result = {"status": "working", "detail": "new fixture response", "ip": "203.0.113.25",
                  "country": "DE", "colo": "FRA", "warp": "off", "submitted": True,
                  "response_verified": True, "request_seen": True, "nonce": NONCE}
        def fail(*args, **kwargs):
            self.fail("browser mode called the curl/API route checker")
        captured = io.StringIO()
        with patch.dict(self.namespace, {"probe_browser": lambda **kwargs: dict(result), "check_route": fail}), \
                patch.dict(os.environ, {"GEMINI_KEY": "invalid and ignored"}), \
                patch.object(sys, "argv", ["gemini-check", "--browser", "--json"]), \
                contextlib.redirect_stdout(captured):
            code = self.namespace["main"]()
        report = json.loads(captured.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(len(report["results"]), 1)
        route = report["results"][0]
        self.assertEqual(route["id"], "browser")
        self.assertEqual(route["browser"]["status"], "working")
        self.assertEqual(route["assessment"]["status"], "web_confirmed")
        self.assertEqual(route["api"]["status"], "not_checked")
        self.assertEqual(route["ip"], "203.0.113.25")

    def test_browser_non_success_exit_codes_do_not_invent_region_bans(self):
        for status, expected in (("region_blocked", 1), ("challenge", 2), ("login_required", 2),
                                 ("timeout", 2), ("unknown", 2), ("dependency_missing", 2)):
            with self.subTest(status=status):
                result = {"status": status, "detail": "fixture", "ip": "203.0.113.25",
                          "country": "DE", "colo": "FRA", "warp": "off", "submitted": False,
                          "response_verified": False, "request_seen": False, "nonce": ""}
                captured = io.StringIO()
                with patch.dict(self.namespace, {"probe_browser": lambda **kwargs: dict(result)}), \
                        patch.object(sys, "argv", ["gemini-check", "--browser", "--json"]), \
                        contextlib.redirect_stdout(captured):
                    code = self.namespace["main"]()
                self.assertEqual(code, expected)
                report = json.loads(captured.getvalue())
                if status != "region_blocked":
                    self.assertNotEqual(report["results"][0]["assessment"]["status"], "restricted")


if __name__ == "__main__":
    unittest.main()
