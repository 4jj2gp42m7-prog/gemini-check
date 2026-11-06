"""IPv4-only CONNECT relay regressions.

All upstream traffic stays on loopback; the optional Chromium test uses a local
TLS server and a test-only certificate exception, never the real Google sites.
"""

import contextlib
import io
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "gemini-check.sh"


def script_namespace():
    source = SCRIPT.read_text(encoding="utf-8")
    source = source.split("<<'PYTHON_GEMINI_CHECK'\n", 1)[1].rsplit("\nPYTHON_GEMINI_CHECK", 1)[0]
    namespace = {"__name__": "gemini_ipv4_test", "__file__": str(SCRIPT)}
    exec(compile(source, str(SCRIPT) + ":python", "exec"), namespace)
    return namespace


class LocalTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        while True:
            try:
                data = self.request.recv(16384)
            except (OSError, socket.timeout):
                return
            if not data:
                return
            self.request.sendall(data)


class ReplyAfterEOF(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        received = bytearray()
        while True:
            try:
                data = self.request.recv(16384)
            except (OSError, socket.timeout):
                return
            if not data:
                break
            received.extend(data)
        self.request.sendall(b"received-until-eof:" + received)


class HTTPReply(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        data = b""
        while b"\r\n\r\n" not in data:
            try:
                chunk = self.request.recv(4096)
            except (OSError, socket.timeout):
                return
            if not chunk:
                return
            data += chunk
        self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\nConnection: close\r\n\r\nIPv4-OK!")


@contextlib.contextmanager
def local_server(handler=Echo):
    server = LocalTCPServer(("127.0.0.1", 0), handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)


def recv_headers(client):
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = client.recv(4096)
        if not chunk:
            return bytes(data), b""
        data.extend(chunk)
        if len(data) > 131072:
            raise AssertionError("proxy did not finish its HTTP headers")
    headers, tail = bytes(data).split(b"\r\n\r\n", 1)
    return headers, tail


def recv_exact(client, count, initial=b""):
    data = bytearray(initial)
    while len(data) < count:
        chunk = client.recv(count - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


class IPv4RelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.namespace = script_namespace()
        if "IPv4BrowserProxy" not in cls.namespace:
            raise AssertionError("IPv4BrowserProxy has not been integrated into the script")
        cls.proxy_type = cls.namespace["IPv4BrowserProxy"]

    def client(self, relay):
        target = urlsplit(relay.server_url)
        self.assertEqual(target.scheme, "http")
        self.assertEqual(target.hostname, "127.0.0.1")
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(3)
        client.connect((target.hostname, target.port))
        self.addCleanup(client.close)
        return client

    def connect(self, relay, host, port, payload=b""):
        client = self.client(relay)
        request = "CONNECT %s:%s HTTP/1.1\r\nHost: %s:%s\r\n\r\n" % (host, port, host, port)
        client.sendall(request.encode("ascii") + payload)
        headers, tail = recv_headers(client)
        return client, headers, tail

    def assert_ok(self, headers):
        self.assertIn(b" 200 ", headers.split(b"\r\n", 1)[0], headers)

    def assert_no_success(self, headers):
        self.assertNotIn(b" 200 ", headers.split(b"\r\n", 1)[0], headers)

    def test_connect_relays_real_tcp_bytes_both_directions(self):
        data = bytes(range(256)) * 80
        with local_server() as server, self.proxy_type() as relay:
            client, headers, tail = self.connect(relay, "127.0.0.1", server.server_address[1])
            self.assert_ok(headers)
            client.sendall(data)
            self.assertEqual(recv_exact(client, len(data), tail), data)
            snapshot = relay.snapshot()
            self.assertEqual(snapshot["family"], "ipv4")
            self.assertTrue(snapshot["connections"])
            connection = snapshot["connections"][-1]
            self.assertEqual(connection["host"], "127.0.0.1")
            self.assertEqual(connection["ip"], "127.0.0.1")
            self.assertEqual(connection["port"], server.server_address[1])

    def test_bytes_after_connect_headers_are_not_lost(self):
        data = b"pipelined-client-bytes\x00\xff"
        with local_server() as server, self.proxy_type() as relay:
            client, headers, tail = self.connect(relay, "127.0.0.1", server.server_address[1], data)
            self.assert_ok(headers)
            self.assertEqual(recv_exact(client, len(data), tail), data)

    def test_client_half_close_preserves_the_upstream_response(self):
        data = b"request-finished"
        with local_server(ReplyAfterEOF) as server, self.proxy_type() as relay:
            client, headers, tail = self.connect(relay, "127.0.0.1", server.server_address[1])
            self.assert_ok(headers)
            client.sendall(data)
            client.shutdown(socket.SHUT_WR)
            expected = b"received-until-eof:" + data
            self.assertEqual(recv_exact(client, len(expected), tail), expected)

    def test_http_response_can_cross_the_connect_tunnel(self):
        with local_server(HTTPReply) as server, self.proxy_type() as relay:
            client, headers, tail = self.connect(relay, "127.0.0.1", server.server_address[1])
            self.assert_ok(headers)
            client.sendall(b"GET /test HTTP/1.1\r\nHost: fixture.test\r\n\r\n")
            response = recv_exact(client, 4096, tail)
            self.assertIn(b"HTTP/1.1 200 OK", response)
            self.assertTrue(response.endswith(b"IPv4-OK!"), response)

    def test_resolver_is_explicitly_limited_to_ipv4(self):
        original = socket.getaddrinfo
        calls = []
        with local_server() as server, self.proxy_type() as relay:
            port = server.server_address[1]

            def resolve(host, target_port, *args, **kwargs):
                family = kwargs.get("family", args[0] if args else socket.AF_UNSPEC)
                kind = kwargs.get("type", args[1] if len(args) > 1 else 0)
                calls.append((host, target_port, family, kind))
                self.assertEqual(family, socket.AF_INET)
                self.assertEqual(kind, socket.SOCK_STREAM)
                return original("127.0.0.1", target_port, socket.AF_INET, socket.SOCK_STREAM)

            with patch("socket.getaddrinfo", side_effect=resolve):
                client, headers, _ = self.connect(relay, "dualstack.example.test", port)
                self.assert_ok(headers)
                client.sendall(b"dns-v4")
                self.assertEqual(recv_exact(client, 6), b"dns-v4")
            self.assertTrue(calls)
            self.assertTrue(all(c[2] == socket.AF_INET for c in calls), calls)
            self.assertEqual(relay.snapshot()["connections"][-1]["host"], "dualstack.example.test")

    def test_ipv6_answer_from_a_misbehaving_resolver_is_not_used(self):
        original = socket.getaddrinfo
        with local_server() as server, self.proxy_type() as relay:
            port = server.server_address[1]

            def resolve(host, target_port, *args, **kwargs):
                family = kwargs.get("family", args[0] if args else socket.AF_UNSPEC)
                self.assertEqual(family, socket.AF_INET)
                ipv6 = (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("::1", target_port, 0, 0))
                return [ipv6] + original("127.0.0.1", target_port, socket.AF_INET, socket.SOCK_STREAM)

            with patch("socket.getaddrinfo", side_effect=resolve):
                client, headers, _ = self.connect(relay, "wrong-resolver.example.test", port)
                self.assert_ok(headers)
                client.sendall(b"only-v4")
                self.assertEqual(recv_exact(client, 7), b"only-v4")
            self.assertTrue(all(ipaddress.ip_address(c["ip"]).version == 4 for c in relay.snapshot()["connections"]))

    def test_ipv6_only_resolver_reply_has_no_ipv6_fallback(self):
        with self.proxy_type() as relay:
            def resolve(host, target_port, *args, **kwargs):
                family = kwargs.get("family", args[0] if args else socket.AF_UNSPEC)
                self.assertEqual(family, socket.AF_INET)
                return [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("::1", target_port, 0, 0))]
            with patch("socket.getaddrinfo", side_effect=resolve):
                _, headers, _ = self.connect(relay, "ipv6-only.example.test", 443)
            self.assert_no_success(headers)
            self.assertEqual(relay.snapshot()["connections"], [])
            self.assertGreaterEqual(relay.snapshot()["failures"], 1)

    def test_ipv6_literal_is_rejected_without_resolving_it(self):
        with self.proxy_type() as relay, patch("socket.getaddrinfo", side_effect=AssertionError("IPv6 literal was resolved")):
            client = self.client(relay)
            client.sendall(b"CONNECT [::1]:443 HTTP/1.1\r\nHost: [::1]:443\r\n\r\n")
            headers, _ = recv_headers(client)
            self.assert_no_success(headers)
            self.assertFalse(relay.snapshot()["connections"])

    def test_unsupported_plain_http_method_is_rejected(self):
        with self.proxy_type() as relay:
            client = self.client(relay)
            client.sendall(b"GET http://example.test/ HTTP/1.1\r\nHost: example.test\r\n\r\n")
            headers, _ = recv_headers(client)
            self.assertIn(b" 405 ", headers.split(b"\r\n", 1)[0], headers)
            self.assertFalse(relay.snapshot()["connections"])

    def test_invalid_connect_port_never_reaches_dns(self):
        for authority in ("example.test:0", "example.test:-1", "example.test:65536",
                          "example.test:not-a-port", "example.test"):
            with self.subTest(authority=authority), self.proxy_type() as relay:
                with patch("socket.getaddrinfo", side_effect=AssertionError("invalid port reached DNS")):
                    client = self.client(relay)
                    client.sendall(("CONNECT %s HTTP/1.1\r\nHost: example.test\r\n\r\n" % authority).encode())
                    headers, _ = recv_headers(client)
                self.assert_no_success(headers)
                self.assertFalse(relay.snapshot()["connections"])

    def test_connect_header_is_bounded(self):
        with self.proxy_type() as relay:
            client = self.client(relay)
            try:
                client.sendall(b"CONNECT example.test:443 HTTP/1.1\r\nX-Long: " + b"x" * 70000)
            except (BrokenPipeError, ConnectionResetError):
                return
            try:
                headers, _ = recv_headers(client)
            except ConnectionResetError:
                headers = b""
            self.assert_no_success(headers)
            self.assertFalse(relay.snapshot()["connections"])

    def test_failed_ipv4_dns_is_a_recorded_failure_not_a_direct_fallback(self):
        with self.proxy_type() as relay, patch("socket.getaddrinfo", side_effect=socket.gaierror("fixture DNS failure")):
            _, headers, _ = self.connect(relay, "unresolved.example.test", 443)
            self.assert_no_success(headers)
            self.assertFalse(relay.snapshot()["connections"])
            self.assertGreaterEqual(relay.snapshot()["failures"], 1)

    def test_proxy_listener_is_closed_on_context_exit(self):
        with self.proxy_type() as relay:
            parsed = urlsplit(relay.server_url)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            self.assertNotEqual(client.connect_ex((parsed.hostname, parsed.port)), 0)

    def test_active_tunnels_are_closed_on_context_exit(self):
        with local_server() as server:
            with self.proxy_type() as relay:
                client, headers, _ = self.connect(relay, "127.0.0.1", server.server_address[1])
                self.assert_ok(headers)
            try:
                self.assertEqual(client.recv(1), b"")
            except ConnectionResetError:
                pass


class IPv4BrowserWrapperTests(unittest.TestCase):
    def setUp(self):
        self.namespace = script_namespace()

    def response(self, ip="203.0.113.25", status="working"):
        return {"status": status, "detail": "fixture response", "ip": ip,
                "country": "DE", "colo": "FRA", "warp": "off",
                "submitted": status == "working", "response_verified": status == "working"}

    def dummy_relay(self, connections, fail_start=False):
        events = []
        class Relay:
            server_url = "http://127.0.0.1:31415"
            def __enter__(self):
                events.append("enter")
                if fail_start:
                    raise OSError("fixture bind failure")
                return self
            def __exit__(self, *args):
                events.append("exit")
            def snapshot(self):
                return {"family": "ipv4", "connections": [dict(c) for c in connections], "failures": 0}
        return Relay, events

    def route(self):
        with patch.dict(os.environ, {"GEMINI_PROXY": ""}), \
                patch.object(sys, "argv", ["gemini-check", "--browser", "--json"]):
            args, _ = self.namespace["arguments"]()
        return self.namespace["check_browser_route"](args)

    def test_default_browser_uses_local_relay_and_records_google_ipv4_evidence(self):
        connections = [{"host": "gemini.google.com", "ip": "142.250.1.1", "port": 443},
                       {"host": "geminiweb-pa.clients6.google.com", "ip": "142.250.1.2", "port": 443}]
        relay, events = self.dummy_relay(connections)
        calls = []
        def session(**kwargs):
            calls.append(kwargs)
            return self.response()
        with patch.dict(self.namespace, {"IPv4BrowserProxy": relay, "_probe_browser_session": session}):
            result = self.namespace["probe_browser"](timeout=7)
        self.assertEqual(result["status"], "working", result)
        self.assertTrue(result["transport"]["google_ipv4_confirmed"])
        self.assertEqual(result["transport"]["family"], "ipv4")
        self.assertEqual(calls, [{"proxy": relay.server_url, "timeout": 7, "headed": False, "profile": None}])
        self.assertEqual(events.count("enter"), 1)
        self.assertEqual(events.count("exit"), 1)

    def test_ipv6_external_address_prevents_an_ipv4_success_even_with_a_reply(self):
        relay, _ = self.dummy_relay([{"host": "gemini.google.com", "ip": "142.250.1.1", "port": 443}])
        with patch.dict(self.namespace, {"IPv4BrowserProxy": relay,
                                        "_probe_browser_session": lambda **kwargs: self.response(ip="2001:db8::25")}):
            route = self.route()
        self.assertTrue(route["browser"]["response_verified"])
        self.assertTrue(route["browser"]["transport"]["google_ipv4_confirmed"])
        self.assertEqual(route["browser"]["status"], "family_mismatch")
        self.assertEqual(route["assessment"]["status"], "inconclusive")

    def test_cloudflare_connection_alone_cannot_confirm_google_ipv4(self):
        for status in ("working", "region_blocked", "access_denied"):
            with self.subTest(status=status):
                relay, _ = self.dummy_relay([{"host": "www.cloudflare.com", "ip": "104.16.1.1", "port": 443}])
                with patch.dict(self.namespace, {"IPv4BrowserProxy": relay,
                                                "_probe_browser_session": lambda **kwargs: self.response(status=status)}):
                    route = self.route()
                self.assertFalse(route["browser"]["transport"]["google_ipv4_confirmed"])
                self.assertEqual(route["browser"]["status"], "routing_unconfirmed")
                self.assertEqual(route["assessment"]["status"], "inconclusive")

    def test_google_connection_with_an_ipv6_address_cannot_confirm_ipv4(self):
        relay, _ = self.dummy_relay([{"host": "gemini.google.com", "ip": "2001:db8::80", "port": 443}])
        with patch.dict(self.namespace, {"IPv4BrowserProxy": relay,
                                        "_probe_browser_session": lambda **kwargs: self.response()}):
            route = self.route()
        self.assertFalse(route["browser"]["transport"]["google_ipv4_confirmed"])
        self.assertEqual(route["browser"]["status"], "routing_unconfirmed")
        self.assertEqual(route["assessment"]["status"], "inconclusive")

    def test_external_proxy_does_not_start_a_local_relay_or_claim_ipv4(self):
        calls = []
        def forbidden():
            self.fail("external proxy mode started the local IPv4 relay")
        def session(**kwargs):
            calls.append(kwargs)
            return self.response(ip="2001:db8::25")
        with patch.dict(self.namespace, {"IPv4BrowserProxy": forbidden, "_probe_browser_session": session}):
            result = self.namespace["probe_browser"](proxy="http://127.0.0.1:3128")
        self.assertEqual(result["status"], "working")
        self.assertEqual(result["transport"]["family"], "proxy")
        self.assertFalse(result["transport"]["google_ipv4_confirmed"])
        self.assertEqual(calls[0]["proxy"], "http://127.0.0.1:3128")

    def test_relay_start_failure_never_falls_back_to_a_direct_connection(self):
        relay, _ = self.dummy_relay([], fail_start=True)
        def forbidden(**kwargs):
            self.fail("browser was launched after IPv4 relay failed")
        with patch.dict(self.namespace, {"IPv4BrowserProxy": relay, "_probe_browser_session": forbidden}):
            result = self.namespace["probe_browser"]()
        self.assertEqual(result["status"], "network_error")
        self.assertFalse(result["submitted"])
        self.assertFalse(result["response_verified"])
        self.assertEqual(result["transport"]["family"], "ipv4")
        self.assertFalse(result["transport"]["google_ipv4_confirmed"])

    def test_explicit_ipv4_with_external_proxy_has_no_silent_fallback(self):
        def forbidden(*args, **kwargs):
            self.fail("unsupported IPv4 proxy combination launched a browser or relay")
        with patch.dict(self.namespace, {"IPv4BrowserProxy": forbidden, "_probe_browser_session": forbidden}):
            result = self.namespace["probe_browser"](proxy="http://127.0.0.1:3128", ipv4=True)
        self.assertEqual(result["status"], "proxy_unsupported")
        self.assertFalse(result["response_verified"])


class IPv4BrowserArgumentsTests(unittest.TestCase):
    def setUp(self):
        self.namespace = script_namespace()

    def arguments(self, *args):
        with patch.dict(os.environ, {}, clear=False), patch.object(sys, "argv", ["gemini-check", *args]):
            original = os.environ.pop("GEMINI_PROXY", None)
            try:
                return self.namespace["arguments"]()
            finally:
                if original is not None:
                    os.environ["GEMINI_PROXY"] = original

    def test_direct_browser_accepts_an_explicit_ipv4_selection(self):
        for mode in ("--browser", "--auto"):
            for family in ("-4", "--ipv4"):
                with self.subTest(mode=mode, family=family):
                    args, key = self.arguments(mode, family, "--json")
                    self.assertTrue(args.browser)
                    self.assertEqual(args.family, "ipv4")
                    self.assertEqual(key, "")

    def test_ipv6_and_both_remain_invalid_for_browser_mode(self):
        for mode in ("--browser", "--auto"):
            for family in ("-6", "--ipv6", "--both"):
                with self.subTest(mode=mode, family=family):
                    with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                        self.arguments(mode, family, "--json")
                    self.assertEqual(caught.exception.code, 64)

    def test_explicit_ipv4_cannot_be_claimed_for_an_external_proxy(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            self.arguments("--browser", "--ipv4", "--proxy", "http://127.0.0.1:3128", "--json")
        self.assertEqual(caught.exception.code, 64)

    def test_browser_route_requests_ipv4_for_its_default_direct_connection(self):
        calls = []
        result = {"status": "unknown", "detail": "fixture", "ip": "", "country": "", "colo": "", "warp": "",
                  "submitted": False, "response_verified": False,
                  "transport": {"family": "ipv4", "connections": [], "failures": 0}}
        args, _ = self.arguments("--browser", "--json")
        with patch.dict(self.namespace, {"probe_browser": lambda **kw: calls.append(kw) or dict(result)}):
            route = self.namespace["check_browser_route"](args)
        self.assertTrue(calls[0]["ipv4"])
        self.assertEqual(route["id"], "browser")

    def test_external_proxy_is_not_mislabelled_as_forced_ipv4(self):
        calls = []
        result = {"status": "unknown", "detail": "fixture", "ip": "", "country": "", "colo": "", "warp": "",
                  "submitted": False, "response_verified": False,
                  "transport": {"family": "proxy", "connections": [], "failures": 0}}
        args, _ = self.arguments("--browser", "--proxy", "http://127.0.0.1:3128", "--json")
        with patch.dict(self.namespace, {"probe_browser": lambda **kw: calls.append(kw) or dict(result)}):
            self.namespace["check_browser_route"](args)
        self.assertFalse(calls[0]["ipv4"])

    def test_auto_ipv4_reaches_the_payload_instead_of_failing_bash_preflight(self):
        with tempfile.TemporaryDirectory(prefix="gemini-ipv4-shell-") as directory:
            root = Path(directory)
            python = root / "python-stub"
            python.write_text("#!/usr/bin/env bash\nif [ \"$1\" = \"-c\" ]; then exit 0; fi\nprintf 'PAYLOAD_STARTED\\n'\n")
            python.chmod(0o700)
            env = dict(os.environ, GEMINI_BROWSER_PYTHON=str(python), GEMINI_BROWSER_HOME=str(root / "runtime"))
            env.pop("GEMINI_PROXY", None)
            completed = subprocess.run(["/bin/bash", str(SCRIPT), "--auto", "--ipv4", "--json"],
                                       env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), "PAYLOAD_STARTED")


@unittest.skipIf(sync_playwright is None or shutil.which("openssl") is None,
                 "the optional Chromium runtime and openssl are required")
class ChromiumIPv4RelayIntegration(unittest.TestCase):
    def test_real_chromium_uses_ipv4_connect_for_trace_and_web_generation(self):
        from test_browser import fixture
        namespace = script_namespace()
        self.assertIn("IPv4BrowserProxy", namespace)
        html = fixture("modern_success")
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, body, content_type):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "https://gemini.google.com:%s" % self.server.server_address[1])
                self.send_header("Access-Control-Allow-Headers", "content-type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                requests.append(("GET", self.headers.get("Host"), self.path))
                if self.path.startswith("/cdn-cgi/trace"):
                    self.respond(b"ip=203.0.113.73\nloc=DE\ncolo=FRA\nwarp=off\n", "text/plain")
                else:
                    self.respond(html.encode(), "text/html")

            def do_OPTIONS(self):
                self.respond(b"", "text/plain")

            def do_POST(self):
                requests.append(("POST", self.headers.get("Host"), self.path))
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.respond(b'{"ok":true}', "application/json")

        with tempfile.TemporaryDirectory(prefix="gemini-ipv4-tls-") as directory:
            root = Path(directory)
            cert, key = root / "cert.pem", root / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                            "-keyout", str(key), "-out", str(cert), "-days", "1",
                            "-subj", "/CN=gemini.test"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.daemon_threads = True
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(str(cert), str(key))
            server.socket = tls.wrap_socket(server.socket, server_side=True)
            server.socket.settimeout(1)
            tls_port = server.server_address[1]
            html = html.replace("https://geminiweb-pa.clients6.google.com/",
                                "https://geminiweb-pa.clients6.google.com:%s/" % tls_port)
            worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            worker.start()
            original_getaddrinfo = socket.getaddrinfo
            resolutions = []

            def resolve(host, port, *args, **kwargs):
                family = kwargs.get("family", args[0] if args else socket.AF_UNSPEC)
                kind = kwargs.get("type", args[1] if len(args) > 1 else 0)
                resolutions.append((host, family, kind))
                self.assertEqual(family, socket.AF_INET)
                self.assertEqual(kind, socket.SOCK_STREAM)
                if host not in ("www.cloudflare.com", "gemini.google.com", "geminiweb-pa.clients6.google.com"):
                    raise socket.gaierror("only fixture hosts are available")
                self.assertEqual(port, tls_port)
                return original_getaddrinfo("127.0.0.1", port, socket.AF_INET, socket.SOCK_STREAM)

            try:
                with sync_playwright() as actual:
                    class Page:
                        def __init__(self, page):
                            self.page = page
                        def __getattr__(self, name):
                            return getattr(self.page, name)
                        def goto(self, url, *args, **kwargs):
                            parsed = urlsplit(url)
                            if parsed.hostname in ("www.cloudflare.com", "gemini.google.com",
                                                   "geminiweb-pa.clients6.google.com"):
                                url = urlunsplit((parsed.scheme, "%s:%s" % (parsed.hostname, tls_port),
                                                 parsed.path, parsed.query, parsed.fragment))
                            return self.page.goto(url, *args, **kwargs)
                    class Context:
                        def __init__(self, context):
                            self.context = context
                        def __getattr__(self, name):
                            return getattr(self.context, name)
                        def new_page(self):
                            return Page(self.context.new_page())
                    class Browser:
                        def __init__(self, browser):
                            self.browser = browser
                        def new_context(self, **kwargs):
                            # Fixture certificates are untrusted; production never sets this.
                            kwargs["ignore_https_errors"] = True
                            return Context(self.browser.new_context(**kwargs))
                        def close(self):
                            self.browser.close()
                    class Chromium:
                        def launch(self, **kwargs):
                            return Browser(actual.chromium.launch(**kwargs))
                    class Driver:
                        def __enter__(self):
                            return SimpleNamespace(chromium=Chromium())
                        def __exit__(self, *args):
                            return False
                    with patch("playwright.sync_api.sync_playwright", return_value=Driver()), \
                            patch("socket.getaddrinfo", side_effect=resolve):
                        result = namespace["probe_browser"](timeout=12, ipv4=True)
                self.assertEqual(result["status"], "working", result)
                self.assertTrue(result["response_verified"], result)
                self.assertEqual(result["ip"], "203.0.113.73", result)
                self.assertEqual(result["transport"]["family"], "ipv4")
                connections = result["transport"]["connections"]
                hosts = {c["host"] for c in connections}
                self.assertIn("www.cloudflare.com", hosts)
                self.assertIn("gemini.google.com", hosts)
                self.assertIn("geminiweb-pa.clients6.google.com", hosts)
                self.assertTrue(all(ipaddress.ip_address(c["ip"]).version == 4 for c in connections))
                self.assertTrue(any(method == "POST" and host.split(":", 1)[0] == "geminiweb-pa.clients6.google.com"
                                    for method, host, _ in requests), requests)
                self.assertTrue(resolutions)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(3)


if __name__ == "__main__":
    unittest.main()
