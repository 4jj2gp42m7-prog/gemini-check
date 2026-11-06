"""A real Chromium navigation failure must not escape the browser deadline."""

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
HAS_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None


# The child owns its browser and all routes. Its intentionally pending main-frame
# navigation reproduces Locator.all() waiting forever after a click timeout.
CHILD = r'''
import json
import sys
import time

sys.path.insert(0, "tests")
from test_browser import fixture, script_namespace
from playwright.sync_api import sync_playwright

namespace = script_namespace()
html = fixture().replace("await fetch(url,", "fetch(url,")
html = html.replace("method: 'POST', headers:", "keepalive: true, method: 'POST', headers:")
html = html.replace("assistant(marker, mode === 'hidden_reply');",
                    "assistant(marker, mode === 'hidden_reply'); location.href='/fixture/stalled-navigation';")
html = html.replace("</script>", """
document.querySelector('button').addEventListener('click', () => window.recordClick(), true);
</script>""")

pending = []
clicks = []
generation_posts = []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(locale="en-US", service_workers="block")
    def respond(route):
        request = route.request
        if request.is_navigation_request() and request.url.endswith('/fixture/stalled-navigation'):
            pending.append(route)
            return
        if request.method == 'POST':
            generation_posts.append(request.url)
            route.fulfill(status=200, content_type='application/json', body='{}')
        else:
            route.fulfill(status=200, content_type='text/html', body=html)
    context.route('**/*', respond)
    page = context.new_page()
    page.expose_function('recordClick', lambda: clicks.append(True))
    try:
        page.goto('https://gemini.google.com/app?hl=en', wait_until='domcontentloaded')
        started = time.monotonic()
        result = namespace['test_browser_page'](page, timeout=6, nonce='CHECK_PENDING_NAV_FIXTURE_3891')
        summary = {'result': result, 'elapsed': time.monotonic() - started,
                   'click_count': len(clicks), 'generation_post_count': len(generation_posts),
                   'pending_navigation_count': len(pending)}
    finally:
        # Release only the local routes this child created, then close its browser.
        for route in pending:
            try:
                route.abort()
            except Exception:
                pass
        context.close()
        browser.close()
print(json.dumps(summary))
'''


@unittest.skipUnless(os.name == "posix", "the external browser watchdog uses an owned POSIX process group")
@unittest.skipUnless(HAS_PLAYWRIGHT, "optional Playwright package is not installed")
class BrowserNavigationDeadlineTests(unittest.TestCase):
    def test_pending_navigation_returns_its_bounded_action_failure(self):
        process = subprocess.Popen([sys.executable, "-c", CHILD], cwd=ROOT,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            # start_new_session gives this test child an exclusive group. Never
            # send a signal to the test runner or another browser process.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = process.communicate(timeout=2)
            self.fail("browser probe exceeded the external 8 s watchdog\n" + stdout + stderr)
        self.assertEqual(process.returncode, 0, stderr)
        summary = json.loads(stdout)
        result = summary["result"]
        self.assertEqual(result["status"], "timeout", result)
        self.assertEqual(result["stage"], "submit", result)
        self.assertEqual(result.get("error", {}).get("reason"), "navigation_interrupted", result)
        self.assertTrue(result["submitted"], result)
        self.assertTrue(result["request_seen"], result)
        self.assertEqual(result["generation_http"], 200, result)
        self.assertFalse(result["response_verified"], result)
        self.assertEqual(summary["click_count"], 1, summary)
        self.assertEqual(summary["generation_post_count"], 1, summary)
        self.assertEqual(summary["pending_navigation_count"], 1, summary)
        self.assertLess(summary["elapsed"], 6, summary)


if __name__ == "__main__":
    unittest.main()
