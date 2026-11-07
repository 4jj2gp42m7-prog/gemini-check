"""Bounded, structured browser error decoding; no browser or network is needed."""

import json
import unittest
from urllib.parse import quote

from test_browser import script_namespace


SECRET = "DO_NOT_EXPORT_RPC_SESSION_539281"


def google_error(code, status, message="The operation was rejected"):
    return {"error": {"code": code, "status": status, "message": message}}


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def webchannel_frame(value):
    # Closure ChannelRequest counts UTF-16 code units in responseText, not bytes.
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    length = len(payload.encode("utf-16-le")) // 2
    return str(length).encode("ascii") + b"\n" + payload.encode("utf-8")


def grpc_trailer(code, message=""):
    payload = ("grpc-status: %s\r\ngrpc-message: %s\r\n" %
               (code, quote(message, safe=""))).encode("ascii")
    return b"\x80" + len(payload).to_bytes(4, "big") + payload


class BrowserRpcErrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.namespace = script_namespace()

    def decode(self, body, content_type="", headers=None):
        parser = self.namespace.get("_browser_rpc_error")
        self.assertTrue(callable(parser), "the structured browser RPC error parser is missing")
        return parser(body, content_type=content_type, headers=headers)

    def assert_error(self, error, code, status, classification=None):
        self.assertIsInstance(error, dict, error)
        self.assertEqual(error.get("code"), code, error)
        self.assertEqual(error.get("status"), status, error)
        self.assertIsInstance(error.get("source"), str, error)
        self.assertTrue(error.get("source"), error)
        self.assertIsInstance(error.get("detail"), str, error)
        self.assertTrue(error.get("detail"), error)
        if classification:
            self.assertEqual(error.get("classification"), classification, error)
        self.assertNotIn(SECRET, json.dumps(error, ensure_ascii=False))

    def test_google_json_keeps_http_error_code_separate_from_rpc_name(self):
        error = self.decode(json_bytes(google_error(403, "PERMISSION_DENIED")), "application/json")
        self.assert_error(error, 403, "PERMISSION_DENIED", "access_denied")

    def test_http_200_webchannel_frame_can_contain_an_explicit_error(self):
        body = webchannel_frame([[1, google_error(429, "RESOURCE_EXHAUSTED")]])
        error = self.decode(body, "text/plain; charset=utf-8")
        self.assert_error(error, 429, "RESOURCE_EXHAUSTED", "quota")

    def test_real_forward_ack_with_seven_is_not_permission_denied(self):
        self.assertIsNone(self.decode(b"8\n[0,0,7]\n", "text/plain; charset=utf-8"))

    def test_webchannel_handshake_is_not_an_error(self):
        body = webchannel_frame([[0, ["c", "FIXTURE_CHANNEL_ID", "", 8]]])
        self.assertIsNone(self.decode(body, "text/plain; charset=utf-8"))

    def test_control_handshake_and_ack_before_error_do_not_hide_the_error(self):
        body = (webchannel_frame([[0, ["c", "FIXTURE_CHANNEL_ID", "", 8]]]) +
                webchannel_frame([0, 0, 7]) +
                webchannel_frame([[2, google_error(401, "UNAUTHENTICATED")]]))
        error = self.decode(body, "text/plain; charset=utf-8")
        self.assert_error(error, 401, "UNAUTHENTICATED", "login_required")

    def test_webchannel_lengths_count_text_not_utf8_bytes(self):
        body = webchannel_frame([[2, google_error(429, "RESOURCE_EXHAUSTED", "Лимит запросов")]])
        self.assert_error(self.decode(body, "text/plain"), 429, "RESOURCE_EXHAUSTED", "quota")

    def test_webchannel_lengths_handle_astral_unicode(self):
        body = webchannel_frame([[2, google_error(429, "RESOURCE_EXHAUSTED", "Quota U0001f6a6 exceeded")]])
        self.assert_error(self.decode(body, "text/plain"), 429, "RESOURCE_EXHAUSTED", "quota")

    def test_grpc_web_trailer_resource_exhaustion_is_a_limit(self):
        error = self.decode(grpc_trailer(8, "Resource exhausted"), "application/grpc-web+proto")
        self.assert_error(error, 8, "RESOURCE_EXHAUSTED", "quota")

    def test_grpc_failed_precondition_alone_never_means_a_region_ban(self):
        error = self.decode(grpc_trailer(9, "A required condition was not met"), "application/grpc-web+proto")
        self.assert_error(error, 9, "FAILED_PRECONDITION")
        self.assertNotEqual(error.get("classification"), "region_blocked", error)

    def test_explicit_unsupported_location_message_is_a_region_error(self):
        body = json_bytes(google_error(400, "FAILED_PRECONDITION", "User location is not supported"))
        self.assert_error(self.decode(body, "application/json"), 400, "FAILED_PRECONDITION", "region_blocked")

    def test_grpc_unauthenticated_requires_login(self):
        error = self.decode(grpc_trailer(16, "Missing credentials"), "application/grpc-web+proto")
        self.assert_error(error, 16, "UNAUTHENTICATED", "login_required")

    def test_grpc_trailers_only_status_in_headers(self):
        error = self.decode(b"", "application/grpc-web+proto",
                            {"grpc-status": "16", "grpc-message": "Missing%20credentials"})
        self.assert_error(error, 16, "UNAUTHENTICATED", "login_required")

    def test_ok_status_is_neither_a_failure_nor_evidence_of_a_model_reply(self):
        self.assertIsNone(self.decode(grpc_trailer(0, "OK"), "application/grpc-web+proto"))

    def test_model_text_and_untyped_code_fields_are_not_error_envelopes(self):
        values = [
            {"code": 7, "text": "model output"},
            {"candidates": [{"content": {"parts": [{"text": "PERMISSION_DENIED code 7"}]}}]},
            {"message": '{"error":{"code":403,"status":"PERMISSION_DENIED"}}'},
            [0, 0, 7],
        ]
        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(self.decode(json_bytes(value), "application/json"))

    def test_error_message_secrets_do_not_enter_controlled_output(self):
        body = json_bytes(google_error(403, "PERMISSION_DENIED", "Account " + SECRET + " may not proceed"))
        self.assert_error(self.decode(body, "application/json"), 403, "PERMISSION_DENIED", "access_denied")

    def test_truncated_or_malformed_frames_do_not_invent_a_code(self):
        values = [
            b"100\n[0,0,7]\n", b"-3\n{}", b"abc\n{}", b"8\n[0,0,", b"\x80\x00\x00\x00\xffgrpc-status: 7",
            b'{"error":', b"7", b"Error code 7", b"\x00\x00\x00\x00\x017",
        ]
        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(self.decode(value, "text/plain"))

    def test_oversized_payload_is_not_scanned_for_error_numbers(self):
        body = json_bytes(google_error(403, "PERMISSION_DENIED", "x" * (2 * 1024 * 1024)))
        self.assertIsNone(self.decode(body, "application/json"))


if __name__ == "__main__":
    unittest.main()
