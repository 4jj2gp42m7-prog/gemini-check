"""Explicit, redacted Google error details without expanding the default report."""

import contextlib
import io
import json
import sys
import unittest
from unittest.mock import patch

from test_browser import script_namespace
from test_browser_output import report


SECRET = "PRIVATE_ERROR_METADATA_58372"
DOMAIN = "geminiweb-pa.clients6.google.com"


def envelope(message="A required condition was not met", details=None):
    value = {"error": {"code": 400, "status": "FAILED_PRECONDITION", "message": message}}
    if details is not None:
        value["error"]["details"] = details
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def error_info(reason):
    return {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason,
            "domain": DOMAIN, "metadata": {"session": SECRET, "project": SECRET}}


class BrowserErrorInfoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.namespace = script_namespace()

    def decode(self, body, **options):
        return self.namespace["_browser_rpc_error"](body, "application/json", **options)

    def test_default_preserves_precondition_meaning_without_message_or_private_details(self):
        body = envelope("Private session " + SECRET, [error_info("FEATURE_DISABLED")])
        value = self.decode(body)
        self.assertEqual(value["code"], 400)
        self.assertEqual(value["status"], "FAILED_PRECONDITION")
        self.assertNotEqual(value["classification"], "region_blocked")
        self.assertIn("услов", value["detail"].lower())
        self.assertNotIn("message", value)
        self.assertNotIn(SECRET, json.dumps(value, ensure_ascii=False))

    def test_explicit_message_is_bounded_and_redacts_tokens_addresses_and_identifiers(self):
        secrets = ["BEARER_VALUE_58372", "COOKIE_VALUE_58372", "SESSION_VALUE_58372",
                   "person@example.invalid", "https://example.invalid/private?token=QUERY_VALUE",
                   "Q7wErTyUiOpAsDfGhJkLzXcVbNm9876543210123456789"]
        jwt = ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
               "KpVH4J2R2Z1Uop3EERnD2_VmZ5-CgUg9S6xOxWf24DI")
        message = ("Required account condition was not met. Authorization: Bearer %s; "
                   "Cookie: session=%s; session_id=%s; email=%s; link=%s; opaque=%s; jwt=%s. " %
                   (*secrets, jwt)) + "Additional diagnostic explanation. " * 50
        value = self.decode(envelope(message), include_message=True)
        self.assertIn("message", value, value)
        self.assertIn("Required account condition was not met", value["message"])
        self.assertLessEqual(len(value["message"]), 350)
        for secret in secrets + [jwt]:
            self.assertNotIn(secret, value["message"])
        self.assertNotIn("\n", value["message"])
        cases = [("Authorization: Bearer ", secrets[0]), ("Cookie: session=", secrets[1]),
                 ("session_id=", secrets[2]), ("Email ", secrets[3]), ("Link ", secrets[4]),
                 ("Opaque ", secrets[5]), ("JWT ", jwt)]
        for prefix, secret in cases:
            with self.subTest(prefix=prefix):
                short = self.decode(envelope("A required condition failed. " + prefix + secret), include_message=True)
                self.assertNotIn(secret, short["message"])
                self.assertIn("A required condition failed", short["message"])

    def test_typed_error_info_exports_only_machine_reason_and_domain(self):
        details = [error_info("FEATURE_DISABLED"),
                   {"@type": "type.googleapis.com/google.rpc.DebugInfo", "detail": SECRET},
                   {"@type": "type.googleapis.com/google.rpc.PreconditionFailure",
                    "violations": [{"type": "REQUIRED", "subject": SECRET, "description": SECRET}]}]
        value = self.decode(envelope(details=details), include_message=True)
        machine = value.get("error_info", value)
        self.assertEqual(machine.get("reason"), "FEATURE_DISABLED", value)
        self.assertEqual(machine.get("domain"), DOMAIN, value)
        encoded = json.dumps(value, ensure_ascii=False)
        self.assertNotIn(SECRET, encoded)
        for key in ("metadata", "subject", "violations", "DebugInfo"):
            self.assertNotIn(key, encoded)

    def test_only_a_known_typed_location_reason_turns_precondition_into_region_failure(self):
        ordinary = self.decode(envelope(details=[error_info("FEATURE_DISABLED")]))
        self.assertNotEqual(ordinary["classification"], "region_blocked")
        location = self.decode(envelope(details=[error_info("USER_LOCATION_NOT_SUPPORTED")]))
        self.assertEqual(location["classification"], "region_blocked", location)
        untyped = self.decode(envelope(details=[{"reason": "USER_LOCATION_NOT_SUPPORTED", "domain": DOMAIN}]))
        self.assertNotEqual(untyped["classification"], "region_blocked", untyped)

    def test_error_info_flag_is_available_for_a_fresh_browser(self):
        with patch.object(sys, "argv", ["gemini-check", "--browser", "--browser-error-info"]):
            args, key = self.namespace["arguments"]()
        self.assertTrue(args.browser)
        self.assertTrue(getattr(args, "browser_error_info", False) or getattr(args, "error_info", False))
        self.assertEqual(key, "")

    def test_error_info_flag_rejects_non_browser_or_persistent_profile(self):
        for options in (("--browser-error-info",),
                        ("--browser", "--browser-error-info", "--browser-profile", "/tmp/private-error-profile")):
            with self.subTest(options=options), patch.object(sys, "argv", ["gemini-check", *options]), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                self.namespace["arguments"]()
            self.assertEqual(raised.exception.code, 64)

    def test_explicit_google_message_keeps_the_brief_report_within_three_lines(self):
        value = self.decode(envelope("A required account condition was not met"), include_message=True)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.namespace["print_report"](report("site_error", response_error=value))
        rendered = output.getvalue()
        self.assertLessEqual(len(rendered.strip().splitlines()), 3, rendered)
        self.assertIn("400", rendered)
        self.assertIn("FAILED_PRECONDITION", rendered)
        self.assertIn(value["message"], rendered)


if __name__ == "__main__":
    unittest.main()
