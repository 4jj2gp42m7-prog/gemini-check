"""The default browser report is short and keeps error layers distinct."""

import contextlib
import io
import os
import sys
import unittest
from unittest.mock import patch

from test_browser import script_namespace


IP = "203.0.113.45"
SECRET = "DO_NOT_PRINT_API_KEY_9527"


def report(status="working", **updates):
    browser = {"status": status, "detail": "Диагностическое описание",
               "country": "DE", "colo": "FRA", "warp": "off", "submitted": True,
               "response_verified": status == "working", "generation_http": 200,
               "request_seen": True, "stage": "wait_answer", "elapsed_ms": 1200,
               "transport": {"family": "ipv4", "google_ipv4_confirmed": True}}
    browser.update(updates)
    route = {"id": "browser", "ip": IP, "browser": browser,
             "assessment": {"status": "inconclusive", "detail": "Работа чата не подтверждена"},
             "warnings": []}
    return {"checked_at": "2026-11-07T00:00:00+00:00", "results": [route]}


class BrowserOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.namespace = script_namespace()

    def render(self, value, verbose=False):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.namespace["print_report"](value, verbose=verbose)
        return output.getvalue()

    def test_success_is_two_lines_without_internal_details_or_an_api_key(self):
        with patch.dict(os.environ, {"GEMINI_KEY": SECRET}):
            output = self.render(report(ignored_api_key=SECRET))
        self.assertEqual(len(output.strip().splitlines()), 2, output)
        self.assertIn(IP, output)
        self.assertIn("Gemini: РАБОТАЕТ", output)
        for value in ("Cloudflare", "ОЦЕНКА", "2026-11-07", "Chromium", SECRET):
            self.assertNotIn(value, output)

    def test_structured_google_and_grpc_errors_show_code_and_meaning_in_three_lines(self):
        for source, code in (("google_json", 403), ("grpc_web", 7)):
            with self.subTest(source=source):
                error = {"source": source, "code": code, "status": "PERMISSION_DENIED",
                         "classification": "access_denied", "detail": "Доступ отклонён; причина по IP не установлена"}
                output = self.render(report("access_denied", response_error=error))
                self.assertLessEqual(len(output.strip().splitlines()), 3, output)
                self.assertIn(IP, output)
                self.assertIn("Код:", output)
                self.assertIn(str(code), output)
                self.assertIn("PERMISSION_DENIED", output)
                self.assertIn(error["detail"], output)
                self.assertNotIn("Gemini: РАБОТАЕТ", output)

    def test_http_200_timeout_never_claims_the_chat_works(self):
        output = self.render(report("timeout"))
        self.assertLessEqual(len(output.strip().splitlines()), 3, output)
        self.assertIn("HTTP 200", output)
        self.assertIn("ответ модели не получен", output)
        self.assertIn("Код ошибки не найден", output)
        self.assertNotIn("Gemini: РАБОТАЕТ", output)

    def test_verbose_keeps_details_and_explicit_diagnostic_data_can_be_shown(self):
        value = report("timeout")
        plain = self.render(value)
        self.assertNotIn("Cloudflare", plain)
        self.assertNotIn("Этап:", plain)
        self.assertNotIn("Текст сайта:", plain)
        detailed = self.render(value, verbose=True)
        self.assertIn("Cloudflare", detailed)
        self.assertIn("Этап:", detailed)
        browser = value["results"][0]["browser"]
        browser["dialogs"] = [{"heading": "Cookie preferences", "buttons": ["Accept all", "Reject all"]}]
        browser["page_info"] = {"lines": ["EXPLICIT_PUBLIC_SITE_MESSAGE"], "user_query_seen": True,
                                "known_assistant_count": 0, "known_nonce_found": False}
        explicit = self.render(value)
        self.assertIn("Cookie preferences", explicit)
        self.assertIn("Accept all", explicit)
        self.assertIn("EXPLICIT_PUBLIC_SITE_MESSAGE", explicit)

    def test_verbose_cli_flag_is_accepted_for_browser_mode(self):
        with patch.object(sys, "argv", ["gemini-check", "--browser", "--verbose"]):
            args, _ = self.namespace["arguments"]()
        self.assertTrue(args.browser)
        self.assertTrue(args.verbose)


if __name__ == "__main__":
    unittest.main()
