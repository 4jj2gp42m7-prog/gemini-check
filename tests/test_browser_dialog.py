"""Blocking Gemini dialogs, using anonymous local Chromium fixtures only."""

import contextlib
import io
import json
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_browser import fixture, script_namespace

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


NONCE = "CHECK_DIALOG_FIXTURE_28419"
PRIVATE_TITLE = "PRIVATE_DIALOG_TITLE_49281"
PRIVATE_BUTTON = "Review PRIVATE_SESSION_TOKEN_19472"
PRIVATE_BODY = "PRIVATE_EXISTING_CHAT_BODY_81914"


@unittest.skipIf(sync_playwright is None, "optional Playwright package is not installed")
class BrowserDialogTests(unittest.TestCase):
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
        self.output = io.StringIO()

    def load(self, html):
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
        self.page.goto("https://gemini.google.com/app?hl=en", wait_until="networkidle")

    def modal(self, attributes='role="dialog"', heading=PRIVATE_TITLE,
              body=PRIVATE_BODY, buttons=None, hidden=False):
        controls = buttons if buttons is not None else '<button type="button">%s</button>' % PRIVATE_BUTTON
        tag = "dialog" if attributes == "native" else "div"
        attrs = "open" if attributes == "native" else attributes
        display = "display:none;" if hidden else ""
        return ('<%s %s id="fixture-dialog" aria-labelledby="fixture-dialog-title" '
                'style="%sposition:fixed;inset:0;z-index:999999;background:white">'
                '<h2 id="fixture-dialog-title">%s</h2><p>%s</p>%s</%s>') % (
                    tag, attrs, display, heading, body, controls, tag)

    def with_modal(self, markup, on_input=False):
        # Keep the real Send button first, because the base fixture wires it up
        # before adding these modal controls.
        setup = """
window.dialogActions = {close:0, reject:0, accept:0, continue:0, other:0, send:0, enter:0};
document.querySelector('button').addEventListener('click', () => window.dialogActions.send++);
editor.addEventListener('keydown', event => {
  if (event.key === 'Enter') window.dialogActions.enter++;
});
function installDialog() {
  if (window.dialogInstalled) return;
  window.dialogInstalled = true;
  document.body.insertAdjacentHTML('beforeend', MODAL_MARKUP);
  const dialog = document.querySelector('#fixture-dialog');
  dialog.querySelectorAll('button').forEach(button => {
    button.addEventListener('click', () => {
      const action = button.getAttribute('data-fixture-action') || 'other';
      window.dialogActions[action]++;
      if (action === 'close' || action === 'reject' || action === 'accept' || action === 'continue') dialog.remove();
    });
  });
}
""".replace("MODAL_MARKUP", json.dumps(markup))
        setup += ("editor.addEventListener('input', installDialog);" if on_input else "installDialog();")
        return fixture("modern_success").replace("</script>", setup + "\n</script>")

    def probe(self, timeout=3, dialog_info=False):
        options = {"dialog_info": True} if dialog_info else {}
        with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output):
            return self.namespace["test_browser_page"](self.page, timeout=timeout, nonce=NONCE, **options)

    def human_report(self, result):
        args = SimpleNamespace(browser_profile=None, proxy="", browser_timeout=3, headed=False,
                               browser_dialog_info=False, dialog_info=False)
        with patch.dict(self.namespace, {"probe_browser": lambda **kwargs: dict(result)}):
            route = self.namespace["check_browser_route"](args)
        with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output):
            self.namespace["print_report"]({"checked_at": "2026-11-07T00:00:00+00:00", "results": [route]})
        return route

    def test_unknown_dialogs_share_detection_and_finish_without_the_action_timeout(self):
        for attributes in ('role="dialog"', 'role="alertdialog"', 'aria-modal="true"', 'native'):
            with self.subTest(attributes=attributes):
                self.load(self.with_modal(self.modal(attributes)))
                started = time.monotonic()
                result = self.probe(timeout=3)
                elapsed = time.monotonic() - started
                self.assertEqual(result["status"], "dialog_required", result)
                self.assertFalse(result["submitted"], result)
                self.assertFalse(result["response_verified"], result)
                self.assertFalse(self.posts, self.posts)
                actions = self.page.evaluate("window.dialogActions")
                self.assertEqual(sum(actions.values()), 0, actions)
                self.assertLess(elapsed, 1.3, "an unknown dialog must not consume the shared action budget")

    def test_unique_neutral_close_can_dismiss_an_otherwise_unknown_popup(self):
        buttons = '<button type="button" aria-label="Close" data-fixture-action="close">×</button>'
        self.load(self.with_modal(self.modal(buttons=buttons)))
        result = self.probe(timeout=3)
        self.assertEqual(result["status"], "working", result)
        self.assertTrue(result["response_verified"], result)
        actions = self.page.evaluate("window.dialogActions")
        self.assertEqual(actions["close"], 1, actions)
        self.assertEqual(actions["send"] + actions["enter"], 1, actions)
        self.assertEqual(actions["accept"], 0, actions)
        self.assertEqual(len(self.posts), 1, self.posts)

    def test_ambiguous_neutral_close_buttons_are_not_clicked(self):
        buttons = ('<button type="button" data-fixture-action="close">Close</button>'
                   '<button type="button" data-fixture-action="close">Dismiss</button>')
        self.load(self.with_modal(self.modal(buttons=buttons)))
        result = self.probe(timeout=3)
        self.assertEqual(result["status"], "dialog_required", result)
        self.assertEqual(sum(self.page.evaluate("window.dialogActions").values()), 0)
        self.assertFalse(self.posts, self.posts)

    def test_close_that_leaves_the_dialog_open_is_not_clicked_repeatedly(self):
        buttons = '<button type="button" data-fixture-action="other">Close</button>'
        self.load(self.with_modal(self.modal(buttons=buttons)))
        started = time.monotonic()
        result = self.probe(timeout=3)
        self.assertEqual(result["status"], "dialog_required", result)
        actions = self.page.evaluate("window.dialogActions")
        self.assertEqual(actions["other"], 1, actions)
        self.assertEqual(actions["send"] + actions["enter"], 0, actions)
        self.assertFalse(self.posts, self.posts)
        self.assertLess(time.monotonic() - started, 1.3)

    def test_cookie_reject_all_after_fill_can_continue_to_one_fresh_reply(self):
        buttons = ('<button type="button" data-fixture-action="reject">Reject all</button>'
                   '<button type="button" data-fixture-action="accept">Accept all</button>')
        markup = self.modal(heading="Before you continue to Google",
                            body="Google uses cookies and data to maintain its services and personalize content.",
                            buttons=buttons)
        self.load(self.with_modal(markup, on_input=True))
        result = self.probe(timeout=4)
        self.assertEqual(result["status"], "working", result)
        self.assertTrue(result["response_verified"], result)
        actions = self.page.evaluate("window.dialogActions")
        self.assertEqual(actions["reject"], 1, actions)
        self.assertEqual(actions["accept"], 0, actions)
        self.assertEqual(actions["send"] + actions["enter"], 1, actions)
        self.assertEqual(len(self.posts), 1, self.posts)

    def test_authorized_terms_agreement_can_continue_to_one_fresh_reply(self):
        buttons = ('<button type="button" data-fixture-action="accept">I agree</button>'
                   '<button type="button" data-fixture-action="other">Continue</button>')
        markup = self.modal(heading="Review the Terms of Service",
                            body="By choosing I agree, you accept the Terms of Service for this account.",
                            buttons=buttons)
        self.load(self.with_modal(markup))
        result = self.probe(timeout=3)
        self.assertEqual(result["status"], "working", result)
        self.assertTrue(result["response_verified"], result)
        actions = self.page.evaluate("window.dialogActions")
        self.assertEqual(actions["accept"], 1, actions)
        self.assertEqual(actions["other"], 0, actions)
        self.assertEqual(actions["send"] + actions["enter"], 1, actions)
        self.assertEqual(len(self.posts), 1, self.posts)

    def test_gemini_privacy_welcome_can_continue_to_one_fresh_reply(self):
        buttons = '<button type="button" data-fixture-action="continue">Continue</button>'
        markup = self.modal(heading="Welcome to Gemini",
                            body="Review the Gemini privacy notice before you start chatting.", buttons=buttons)
        self.load(self.with_modal(markup))
        result = self.probe(timeout=3)
        self.assertEqual(result["status"], "working", result)
        self.assertTrue(result["response_verified"], result)
        actions = self.page.evaluate("window.dialogActions")
        self.assertEqual(actions["continue"], 1, actions)
        self.assertEqual(actions["send"] + actions["enter"], 1, actions)
        self.assertEqual(len(self.posts), 1, self.posts)

    def test_one_dialog_node_can_advance_from_welcome_to_terms_before_chat(self):
        buttons = '<button type="button" data-fixture-action="continue">Continue</button>'
        markup = self.modal(heading="Welcome to Gemini",
                            body="Review the Gemini privacy notice before you start chatting.", buttons=buttons)
        html = self.with_modal(markup).replace(
            "if (action === 'close' || action === 'reject' || action === 'accept' || action === 'continue') dialog.remove();",
            """if (action === 'continue') {
        dialog.querySelector('h2').textContent = 'Review the Terms of Service';
        dialog.querySelector('p').textContent = 'Choose I agree to accept the Gemini Terms of Service.';
        button.textContent = 'I agree';
        button.setAttribute('data-fixture-action', 'accept');
      } else if (action === 'accept') {
        dialog.remove();
      }""")
        self.load(html)
        result = self.probe(timeout=4)
        self.assertEqual(result["status"], "working", result)
        self.assertTrue(result["response_verified"], result)
        actions = self.page.evaluate("window.dialogActions")
        self.assertEqual(actions["continue"], 1, actions)
        self.assertEqual(actions["accept"], 1, actions)
        self.assertEqual(actions["send"] + actions["enter"], 1, actions)
        self.assertEqual(result.get("diagnostics", {}).get("dialog_action_attempts"), 2, result)
        self.assertEqual(len(self.posts), 1, self.posts)

    def test_cookie_accept_all_is_available_when_reject_all_is_absent(self):
        buttons = '<button type="button" data-fixture-action="accept">Accept all</button>'
        markup = self.modal(heading="Before you continue to Google",
                            body="Google uses cookies and data to maintain its services.", buttons=buttons)
        self.load(self.with_modal(markup))
        result = self.probe(timeout=3)
        self.assertEqual(result["status"], "working", result)
        self.assertTrue(result["response_verified"], result)
        actions = self.page.evaluate("window.dialogActions")
        self.assertEqual(actions["accept"], 1, actions)
        self.assertEqual(actions["reject"], 0, actions)
        self.assertEqual(actions["send"] + actions["enter"], 1, actions)
        self.assertEqual(len(self.posts), 1, self.posts)

    def test_payment_start_trial_and_continue_buttons_are_not_accepted(self):
        buttons = ('<button type="button" data-fixture-action="accept">Start trial</button>'
                   '<button type="button" data-fixture-action="continue">Continue</button>')
        markup = self.modal(heading="Try Gemini Advanced",
                            body="Start your free trial. Add a payment method to continue.", buttons=buttons)
        self.load(self.with_modal(markup))
        started = time.monotonic()
        result = self.probe(timeout=3)
        self.assertNotIn(result["status"], ("working", "timeout", "region_blocked"), result)
        self.assertFalse(result["submitted"], result)
        self.assertEqual(sum(self.page.evaluate("window.dialogActions").values()), 0)
        self.assertFalse(self.posts, self.posts)
        self.assertLess(time.monotonic() - started, 1.3)

    def test_default_dialog_diagnostics_do_not_export_private_titles_or_buttons(self):
        self.load(self.with_modal(self.modal()))
        result = self.probe(timeout=3)
        route = self.human_report(result)
        self.assertEqual(route["assessment"]["status"], "inconclusive", route)
        exported = json.dumps(result, ensure_ascii=False) + self.output.getvalue()
        for private in (PRIVATE_TITLE, PRIVATE_BUTTON, PRIVATE_BODY):
            self.assertNotIn(private, exported)

    def test_explicit_dialog_info_includes_only_heading_and_button_labels(self):
        self.load(self.with_modal(self.modal()))
        result = self.probe(timeout=3, dialog_info=True)
        self.assertEqual(result["status"], "dialog_required", result)
        self.assertIn("dialogs", result, result)
        exported = json.dumps(result["dialogs"], ensure_ascii=False)
        self.assertIn(PRIVATE_TITLE, exported)
        self.assertIn(PRIVATE_BUTTON, exported)
        self.assertNotIn(PRIVATE_BODY, exported)
        self.assertFalse(self.posts, self.posts)

    def test_hidden_dialog_does_not_block_the_chat(self):
        self.load(self.with_modal(self.modal(hidden=True)))
        result = self.probe(timeout=3)
        self.assertEqual(result["status"], "working", result)
        self.assertTrue(result["response_verified"], result)
        self.assertEqual(sum(self.page.evaluate("window.dialogActions").values()), 1)
        self.assertEqual(len(self.posts), 1, self.posts)


class DialogInfoCliTests(unittest.TestCase):
    def setUp(self):
        self.namespace = script_namespace()

    def test_dialog_info_is_available_in_a_fresh_browser(self):
        with patch.object(sys, "argv", ["gemini-check", "--browser", "--browser-dialog-info"]):
            args, key = self.namespace["arguments"]()
        self.assertTrue(args.browser)
        self.assertTrue(getattr(args, "browser_dialog_info", False) or getattr(args, "dialog_info", False))
        self.assertEqual(key, "")

    def test_dialog_info_rejects_a_persistent_browser_profile(self):
        with patch.object(sys, "argv", ["gemini-check", "--browser", "--browser-dialog-info",
                                      "--browser-profile", "/tmp/private-gemini-profile"]), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            self.namespace["arguments"]()
        self.assertEqual(raised.exception.code, 64)

    def test_dialog_info_requires_the_browser_mode(self):
        with patch.object(sys, "argv", ["gemini-check", "--browser-dialog-info"]), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            self.namespace["arguments"]()
        self.assertEqual(raised.exception.code, 64)


if __name__ == "__main__":
    unittest.main()
