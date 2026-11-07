"""Passive CDP stream-monitor regressions, with no browser or network traffic."""

import base64
import json
from types import SimpleNamespace
import unittest

from test_browser import script_namespace


ENDPOINT = "https://geminiweb-pa.clients6.google.com/v1/processSession"
SECRET = "DO_NOT_EXPORT_MONITOR_MESSAGE_91827"


def error_body(code=403, status="PERMISSION_DENIED"):
    return json.dumps({"error": {"code": code, "status": status, "message": SECRET}}).encode()


class FakeCDP:
    def __init__(self):
        self.listeners = {}
        self.plans = {}
        self.calls = []
        self.detach_count = 0

    def on(self, event, listener):
        self.listeners.setdefault(event, []).append(listener)

    def remove_listener(self, event, listener):
        self.listeners[event].remove(listener)

    def emit(self, event, values):
        for listener in list(self.listeners.get(event, [])):
            listener(values)

    def send(self, method, values):
        self.calls.append((method, dict(values)))
        if method == "Network.enable":
            return {}
        if method != "Network.streamResourceContent":
            raise AssertionError("unexpected network operation: " + method)
        rid = values["requestId"]
        plan = self.plans.get(rid, {})
        if plan.get("error"):
            raise RuntimeError(plan["error"])
        # CDP can deliver these while the synchronous send still awaits its reply.
        for chunk in plan.get("reentrant", []):
            self.emit("Network.dataReceived", {"requestId": rid, "data": base64.b64encode(chunk).decode()})
        return {"bufferedData": base64.b64encode(plan.get("buffered", b"")).decode()}

    def detach(self):
        self.detach_count += 1


class BrowserResponseMonitorTests(unittest.TestCase):
    def setUp(self):
        self.namespace = script_namespace()
        self.session = FakeCDP()
        self.other_session = FakeCDP()
        self.state = {"sending": False}
        self.session_requests = []

        def new_session(page):
            self.session_requests.append(page)
            return self.session

        self.page = SimpleNamespace(context=SimpleNamespace(new_cdp_session=new_session))
        self.monitor = self.namespace["BrowserResponseMonitor"](self.page, self.state)
        self.addCleanup(self.monitor.close)

    def start(self, rid="request-1", buffered=b"", reentrant=(), url=ENDPOINT):
        self.session.plans[rid] = {"buffered": buffered, "reentrant": list(reentrant)}
        self.session.emit("Network.requestWillBeSent", {"requestId": rid, "request": {"url": url, "method": "GET"}})
        self.session.emit("Network.responseReceived", {"requestId": rid, "response": {
            "url": url, "mimeType": "text/plain", "headers": {"content-type": "text/plain", "set-cookie": SECRET}}})

    def chunk(self, body, rid="request-1"):
        self.session.emit("Network.dataReceived", {"requestId": rid, "data": base64.b64encode(body).decode()})

    def test_pre_submit_error_is_ignored_and_new_armed_data_is_decoded(self):
        self.start(buffered=error_body())
        self.assertTrue(self.monitor.available)
        self.assertFalse(self.monitor.errors)
        self.state["sending"] = True
        self.chunk(error_body(429, "RESOURCE_EXHAUSTED"))
        self.assertEqual(len(self.monitor.errors), 1)
        self.assertEqual(self.monitor.errors[0]["code"], 429)
        self.assertEqual(self.monitor.errors[0]["classification"], "quota")
        self.assertNotIn(SECRET, json.dumps(self.monitor.errors))
        self.assertNotIn("set-cookie", self.monitor._records["request-1"]["headers"])

    def test_buffered_prefix_precedes_data_delivered_inside_enable_reply(self):
        self.state["sending"] = True
        payload = error_body()
        frame = str(len(payload)).encode() + b"\n" + payload + b"\n"
        self.start(buffered=frame[:9], reentrant=(frame[9:20], frame[20:]))
        self.assertEqual(bytes(self.monitor._records["request-1"]["buffer"]), frame)
        self.assertEqual(len(self.monitor.errors), 1)
        self.assertEqual(self.monitor.errors[0]["code"], 403)
        self.assertEqual(self.monitor.errors[0]["source"], "webchannel_json")
        self.assertNotIn(SECRET, json.dumps(self.monitor.errors))

    def test_unsupported_streaming_method_is_silent_and_detaches_owned_session(self):
        self.state["sending"] = True
        self.session.plans["unsupported"] = {"error": "Protocol error: Network.streamResourceContent wasn't found"}
        self.session.emit("Network.requestWillBeSent", {"requestId": "unsupported", "request": {"url": ENDPOINT, "method": "GET"}})
        self.session.emit("Network.responseReceived", {"requestId": "unsupported", "response": {"url": ENDPOINT, "headers": {}}})
        self.assertFalse(self.monitor.available)
        self.assertFalse(self.monitor.errors)
        self.assertEqual(self.session.detach_count, 1)
        self.assertEqual(self.other_session.detach_count, 0)
        self.monitor.close()
        self.assertEqual(self.session.detach_count, 1)

    def test_real_successful_channel_ack_does_not_become_rpc_code_seven(self):
        self.state["sending"] = True
        self.start(buffered=b"8\n[0,0,7]\n")
        self.assertTrue(self.monitor.available)
        self.assertFalse(self.monitor.errors)

    def test_stream_buffers_and_request_count_have_hard_caps(self):
        self.state["sending"] = True
        huge = b"x" * (self.monitor.MAX_BYTES + 20000)
        for index in range(self.monitor.MAX_REQUESTS + 3):
            self.start("request-%s" % index, buffered=huge, reentrant=(huge,))
        self.assertEqual(len(self.monitor._starts), 16)
        self.assertEqual(len(self.monitor._records), 16)
        self.assertEqual(len([call for call in self.session.calls if call[0] == "Network.streamResourceContent"]), 16)
        for record in self.monitor._records.values():
            self.assertLessEqual(len(record["buffer"]), 65536)
            self.assertLessEqual(len(record["queue"]), 65536)
        self.assertFalse(self.monitor.errors)

    def test_close_removes_only_owned_listeners_and_does_not_intercept_requests(self):
        extra_listener = lambda event: None
        self.session.on("Network.dataReceived", extra_listener)
        self.state["sending"] = True
        self.start(url="https://unrelated.example.invalid/v1/processSession")
        self.assertEqual(len(self.monitor._records), 0)
        self.assertEqual(self.session_requests, [self.page])
        self.monitor.close()
        self.assertEqual(self.session.detach_count, 1)
        self.assertEqual(self.other_session.detach_count, 0)
        self.assertEqual(self.session.listeners["Network.dataReceived"], [extra_listener])
        self.assertFalse(self.session.listeners["Network.responseReceived"])
        self.assertFalse(self.session.listeners["Network.requestWillBeSent"])
        self.assertEqual([name for name, _ in self.session.calls], ["Network.enable"])


if __name__ == "__main__":
    unittest.main()
