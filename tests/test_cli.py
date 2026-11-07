"""Black-box CLI regression tests. All network traffic uses a temporary curl stub."""

import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "gemini-check.sh"
TEST_KEY = "test-key-PRIVATE-123456"
TEST_PROXY = "socks5h://user:PRIVATE-proxy-password@127.0.0.1:1080"


CURL_STUB = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import shlex
import sys

args = sys.argv[1:]
if "--version" in args:
    print("curl 8.5.0 (test) OpenSSL/3.0.0")
    raise SystemExit(0)
config = sys.stdin.read() if "--config" in args else ""
request_key = ""
for line in config.splitlines():
    if line.strip().startswith("header") and "=" in line:
        values = shlex.split(line.split("=", 1)[1].strip())
        if values and values[0].lower().startswith("x-goog-api-key:"):
            request_key = values[0].split(":", 1)[1].strip()
with open(os.environ["GEMINI_TEST_CURL_LOG"], "a") as log:
    log.write(json.dumps({"argv": args, "config": config}) + "\n")

def option(*names):
    for name in names:
        if name in args:
            return args[args.index(name) + 1]
    return None

url = args[-1]
scenario = os.environ.get("GEMINI_TEST_SCENARIO", "normal")
ipv6 = "-6" in args or "--ipv6" in args
ip = "2001:db8::10" if ipv6 else "203.0.113.10"
if scenario == "ipv6_translated_to_ipv4" and ipv6:
    ip = "203.0.113.10"
status = 200
effective = url
curl_code = 0
body = ""
headers = "Content-Type: application/json\r\n"

def error(code, message, status_name="FAILED_PRECONDITION", details=None):
    data = {"code": code, "message": message, "status": status_name}
    if details is not None:
        data["details"] = details
    return json.dumps({"error": data})

if scenario == "all_network" or (scenario == "ipv6_unavailable" and ipv6):
    curl_code = 7
    status = 0
elif "ipinfo.io/json" in url:
    if scenario == "ipinfo_failure":
        status = 429
        body = '{"error":{"message":"Rate limit exceeded"}}'
    elif scenario == "ipinfo_html":
        body = "<html>upstream unavailable</html>"
    else:
        body = json.dumps({"ip": ip, "country": "DE", "city": "Berlin", "org": "AS64500 Example Network"})
elif "cloudflare.com/cdn-cgi/trace" in url:
    if scenario == "cloudflare_failure":
        status = 0
        curl_code = 28
    else:
        cf_ip = "203.0.113.11" if scenario == "different_egress" else ip
        body = "fl=test\nip=%s\nloc=DE\ncolo=FRA\nwarp=off\n" % cf_ip
        if scenario == "cloudflare_crlf":
            body = body.replace("\n", "\r\n")
        headers = "Content-Type: text/plain\r\n"
elif "youtube.com/" in url:
    country = "RU" if scenario == "youtube_ru" else "DE"
    body = '<html><script>var data={"GL":"%s"};</script></html>' % country
    if scenario == "youtube_missing":
        body = "<html>Consent required</html>"
    headers = "Content-Type: text/html\r\n"
elif "gemini.google.com/" in url:
    body = '<!doctype html><html><head><title>Gemini</title></head><body><main>Chat with Gemini</main><script>window.WIZ_global_data={"cfb2h":"boq_assistant-bard-web-server"};</script></body></html>'
    if scenario in ("web_region", "web_region_api_success"):
        body = "<html><body><main>Gemini isn't available in your country</main></body></html>"
    elif scenario == "region_script_only":
        body += '<script>window.translations={"blocked":"Gemini isn\'t available in your country"};</script>'
    elif scenario in ("region_hidden", "region_aria_hidden", "region_display_none"):
        attributes = {"region_hidden": "hidden", "region_aria_hidden": 'aria-hidden="true"', "region_display_none": 'style="display: none"'}[scenario]
        body += '<div %s><span>Gemini isn\'t available in your country</span></div>' % attributes
    elif scenario == "web_challenge":
        status = 429
        effective = "https://www.google.com/sorry/index?continue=https://gemini.google.com/"
        body = "<html><body>Our systems have detected unusual traffic from your computer network</body></html>"
    elif scenario == "web_login":
        effective = "https://accounts.google.com/ServiceLogin?service=bard"
        body = "<html><body>Sign in with your Google Account</body></html>"
    elif scenario == "web_consent":
        effective = "https://consent.google.com/m"
        body = "<html><body>Before you continue to Google</body></html>"
    elif scenario == "web_forbidden":
        status = 403
        body = "<html><body>Forbidden</body></html>"
    elif scenario == "web_missing":
        status = 404
        body = "<html><body>Not found</body></html>"
    elif scenario == "web_overloaded":
        status = 503
        body = "<html><body>Service temporarily unavailable</body></html>"
    elif scenario == "unrelated_html":
        body = "<html><title>Gemini</title><body>Maintenance</body></html>"
    headers = "Content-Type: text/html\r\n"
elif "generativelanguage.googleapis.com/" in url:
    if ":generateContent" not in url:
        if scenario == "api_region_list":
            status = 400
            body = error(400, "User location is not supported for the API use.")
        elif scenario == "no_models":
            body = '{"models":[]}'
        elif scenario == "list_html":
            body = "<html>Gateway unavailable</html>"
        else:
            body = json.dumps({"models": [
                {"name": "models/gemini-flash-lite-latest", "supportedGenerationMethods": ["generateContent", "countTokens"]},
                {"name": "models/gemini-2.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-2.0-flash-lite", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-embedding-test", "supportedGenerationMethods": ["embedContent"]}
            ]})
    else:
        if scenario == "api_region":
            status = 400
            body = error(400, "User location is not supported for the API use.")
        elif scenario == "api_current_location":
            status = 400
            body = error(400, "This API is not available in your current location.")
        elif scenario == "quota":
            status = 429
            body = error(429, "You exceeded your current quota. Please check your plan and billing details.", "RESOURCE_EXHAUSTED", [
                {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests"}]}
            ])
        elif scenario == "overloaded":
            status = 503
            body = error(503, "The model is overloaded. Please try again later.", "UNAVAILABLE")
        elif scenario == "quota_html":
            status = 429
            body = "<html><body>Too many requests</body></html>"
        elif scenario == "billing":
            status = 400
            body = error(400, "Gemini API free tier is not available in your country. Please enable billing on your project in Google AI Studio.")
        elif scenario == "key_error":
            status = 400
            body = error(400, "API key not valid. Please pass a valid API key.", "INVALID_ARGUMENT", [
                {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID", "domain": "googleapis.com"}
            ])
        elif scenario == "permission":
            status = 403
            body = error(403, "Your API key does not have permission to use this resource.", "PERMISSION_DENIED")
        elif scenario == "reflected_key":
            status = 403
            body = error(403, "API key " + request_key + " does not have permission.", "PERMISSION_DENIED")
        elif scenario == "api_html":
            body = "<html><body>Gemini login</body></html>"
        elif scenario == "api_empty":
            body = '{"candidates":[]}'
        elif scenario == "api_nonobject":
            body = "[]"
        elif scenario == "api_whitespace":
            body = '{"candidates":[{"content":{"parts":[{"text":"   "}]}}]}'
        elif scenario == "fallback_404" and "gemini-flash-lite-latest:" in url:
            status = 404
            body = error(404, "models/gemini-flash-lite-latest is not found", "NOT_FOUND")
        elif scenario == "temperature_unsupported" and "temperature" in (option("--data-binary", "--data", "-d") or ""):
            status = 400
            body = error(400, "Unsupported generationConfig.temperature for this model", "INVALID_ARGUMENT")
        else:
            model_version = "gemini-3.0-flash-lite" if scenario == "model_version_changed" else "gemini-2.5-flash-lite"
            body = json.dumps({"candidates": [{"content": {"parts": [{"text": "OK"}]}, "finishReason": "STOP"}], "modelVersion": model_version})
else:
    raise SystemExit("Unexpected test URL: " + url)

output = option("--output", "-o")
if output and output != "/dev/null":
    Path(output).write_text(body)
elif output != "/dev/null":
    sys.stdout.write(body)
header_file = option("--dump-header", "-D")
if header_file:
    Path(header_file).write_text("HTTP/1.1 %s Test\r\n%s\r\n" % (status, headers))
writeout = option("--write-out", "-w")
if writeout:
    values = {"http_code": "%03d" % status, "url_effective": effective, "remote_ip": "2001:db8::20" if ipv6 else "192.0.2.20", "time_total": "0.012"}
    for key, value in values.items():
        writeout = writeout.replace("%{" + key + "}", value)
    sys.stdout.write(writeout.replace("\\n", "\n"))
raise SystemExit(curl_code)
'''


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gemini-check-tests-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        curl = self.bin / "curl"
        curl.write_text(CURL_STUB)
        curl.chmod(0o755)
        self.log = self.directory / "curl.jsonl"

    def invoke(self, *args, scenario="normal", api=False, environment=None, json_output=True):
        env = dict(os.environ)
        for key in ("GEMINI_KEY", "GEMINI_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            env.pop(key, None)
        env.update({"PATH": str(self.bin) + os.pathsep + env.get("PATH", ""),
                    "GEMINI_KEY": TEST_KEY if api else "-",
                    "GEMINI_TEST_SCENARIO": scenario,
                    "GEMINI_TEST_CURL_LOG": str(self.log)})
        if environment:
            env.update(environment)
        command = ["/bin/bash", str(SCRIPT)]
        if json_output:
            command.append("--json")
        command.extend(args)
        completed = subprocess.run(command, env=env, text=True, capture_output=True, timeout=20)
        if not json_output:
            return completed, None
        try:
            report = json.loads(completed.stdout)
        except ValueError:
            self.fail("Expected a JSON report, exit=%s, stdout=%r, stderr=%r" % (completed.returncode, completed.stdout, completed.stderr))
        self.assertIsInstance(report, dict)
        self.assertEqual(report.get("schema_version"), 1)
        self.assertEqual(str(report.get("version")), "2.7")
        self.assertTrue(report.get("checked_at"))
        self.assertIsInstance(report.get("results"), list)
        self.assertIsInstance(report.get("changes"), list)
        return completed, report

    def only(self, report):
        self.assertEqual(len(report["results"]), 1)
        return report["results"][0]

    def requests(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def generation_requests(self):
        return [entry for entry in self.requests() if ":generateContent" in entry["argv"][-1]]

    def test_no_key_does_not_confirm_gemini_from_youtube_country(self):
        completed, report = self.invoke("--ipv4", "--no-api")
        self.assertEqual(completed.returncode, 2)
        result = self.only(report)
        self.assertEqual(result["youtube"]["country"], "DE")
        self.assertEqual(result["api"]["status"], "not_checked")
        self.assertEqual(result["assessment"]["status"], "inconclusive")
        self.assertFalse(self.generation_requests())

    def test_successful_generation_is_not_overruled_by_youtube_ru(self):
        _, report = self.invoke("-4", scenario="youtube_ru", api=True)
        result = self.only(report)
        self.assertEqual(result["api"]["status"], "working")
        self.assertIn(result["assessment"]["status"], ("api_confirmed", "mixed"))
        self.assertEqual(result["youtube"]["country"], "RU")

    def test_actual_generation_confirms_api(self):
        completed, report = self.invoke("--ipv4", api=True)
        self.assertEqual(completed.returncode, 0)
        result = self.only(report)
        self.assertEqual(result["api"]["status"], "working")
        self.assertEqual(result["assessment"]["status"], "api_confirmed")
        self.assertGreaterEqual(len(result["api"]["attempts"]), 1)

    def test_explicit_api_region_refusal(self):
        completed, report = self.invoke("-4", scenario="api_region", api=True)
        self.assertEqual(completed.returncode, 1)
        result = self.only(report)
        self.assertEqual(result["api"]["status"], "region_blocked")
        self.assertEqual(result["assessment"]["status"], "restricted")

    def test_models_list_region_refusal_stops_generation(self):
        _, report = self.invoke("-4", scenario="api_region_list", api=True)
        self.assertEqual(self.only(report)["api"]["status"], "region_blocked")
        self.assertFalse(self.generation_requests())

    def test_current_location_api_refusal_is_recognized(self):
        _, report = self.invoke("-4", scenario="api_current_location", api=True)
        result = self.only(report)
        self.assertEqual(result["api"]["status"], "region_blocked")
        self.assertEqual(result["assessment"]["status"], "restricted")

    def test_quota_is_not_a_success_or_region_ban(self):
        completed, report = self.invoke("-4", scenario="quota", api=True)
        self.assertEqual(completed.returncode, 2)
        result = self.only(report)
        self.assertEqual(result["api"]["status"], "quota")
        self.assertEqual(result["assessment"]["status"], "inconclusive")
        self.assertEqual(len(self.generation_requests()), 1)

    def test_overload_is_not_a_success_or_region_ban(self):
        _, report = self.invoke("-4", scenario="overloaded", api=True)
        result = self.only(report)
        self.assertEqual(result["api"]["status"], "overloaded")
        self.assertEqual(result["assessment"]["status"], "inconclusive")
        self.assertLessEqual(len(self.generation_requests()), 2)

    def test_non_json_http_429_is_still_a_quota_error(self):
        _, report = self.invoke("-4", scenario="quota_html", api=True)
        result = self.only(report)
        self.assertEqual(result["api"]["status"], "quota")
        self.assertEqual(result["assessment"]["status"], "inconclusive")

    def test_billing_country_message_is_not_region_ban(self):
        _, report = self.invoke("-4", scenario="billing", api=True)
        result = self.only(report)
        self.assertEqual(result["api"]["status"], "billing")
        self.assertEqual(result["assessment"]["status"], "inconclusive")

    def test_invalid_api_key(self):
        _, report = self.invoke("-4", scenario="key_error", api=True)
        self.assertEqual(self.only(report)["api"]["status"], "key_error")

    def test_api_permission_error_is_not_region_ban(self):
        _, report = self.invoke("-4", scenario="permission", api=True)
        result = self.only(report)
        self.assertEqual(result["api"]["status"], "permission_denied")
        self.assertEqual(result["assessment"]["status"], "inconclusive")

    def test_http_200_is_not_enough_for_api_success(self):
        for scenario in ("api_html", "api_empty", "api_nonobject", "api_whitespace"):
            with self.subTest(scenario=scenario):
                _, report = self.invoke("-4", scenario=scenario, api=True)
                result = self.only(report)
                self.assertNotEqual(result["api"]["status"], "working")
                self.assertEqual(result["assessment"]["status"], "inconclusive")

    def test_no_usable_model_is_explicit(self):
        _, report = self.invoke("-4", scenario="no_models", api=True)
        self.assertEqual(self.only(report)["api"]["status"], "no_model")
        self.assertFalse(self.generation_requests())

    def test_models_html_is_invalid_not_empty_success(self):
        _, report = self.invoke("-4", scenario="list_html", api=True)
        self.assertEqual(self.only(report)["api"]["status"], "invalid_response")
        self.assertFalse(self.generation_requests())

    def test_model_override_bypasses_model_discovery(self):
        _, report = self.invoke("-4", "--model", "gemini-custom-lite", api=True)
        self.assertEqual(self.only(report)["api"]["status"], "working")
        calls = self.requests()
        self.assertFalse(any("models?" in entry["argv"][-1] for entry in calls))
        self.assertTrue(any("models/gemini-custom-lite:generateContent" in entry["argv"][-1] for entry in calls))

    def test_probe_does_not_require_optional_model_parameters(self):
        _, report = self.invoke("-4", scenario="temperature_unsupported", api=True)
        self.assertEqual(self.only(report)["api"]["status"], "working")

    def test_limited_fallback_for_missing_model(self):
        _, report = self.invoke("-4", scenario="fallback_404", api=True)
        self.assertEqual(self.only(report)["api"]["status"], "working")
        self.assertGreaterEqual(len(self.generation_requests()), 1)
        self.assertLessEqual(len(self.generation_requests()), 2)

    def test_visible_region_message_is_restricted(self):
        _, report = self.invoke("-4", scenario="web_region")
        result = self.only(report)
        self.assertEqual(result["web"]["status"], "region_blocked")
        self.assertEqual(result["assessment"]["status"], "restricted")

    def test_web_region_and_working_api_are_reported_separately(self):
        completed, report = self.invoke("-4", scenario="web_region_api_success", api=True)
        self.assertEqual(completed.returncode, 1)
        result = self.only(report)
        self.assertEqual(result["web"]["status"], "region_blocked")
        self.assertEqual(result["api"]["status"], "working")
        self.assertEqual(result["assessment"]["status"], "mixed")

    def test_translations_in_scripts_are_not_active_restrictions(self):
        _, report = self.invoke("-4", scenario="region_script_only")
        result = self.only(report)
        self.assertNotEqual(result["web"]["status"], "region_blocked")
        self.assertEqual(result["assessment"]["status"], "inconclusive")

    def test_hidden_dom_region_messages_are_not_active_restrictions(self):
        for scenario in ("region_hidden", "region_aria_hidden", "region_display_none"):
            with self.subTest(scenario=scenario):
                _, report = self.invoke("-4", scenario=scenario)
                result = self.only(report)
                self.assertNotEqual(result["web"]["status"], "region_blocked")
                self.assertEqual(result["assessment"]["status"], "inconclusive")

    def test_redirects_distinguish_login_consent_and_challenge(self):
        for scenario, expected in (("web_login", "login_required"), ("web_consent", "consent"), ("web_challenge", "challenge")):
            with self.subTest(scenario=scenario):
                _, report = self.invoke("-4", scenario=scenario)
                result = self.only(report)
                self.assertEqual(result["web"]["status"], expected)
                self.assertNotEqual(result["assessment"]["status"], "api_confirmed")

    def test_bare_http_error_and_overload_do_not_mean_region_ban(self):
        for scenario in ("web_forbidden", "web_overloaded"):
            with self.subTest(scenario=scenario):
                completed, report = self.invoke("-4", scenario=scenario)
                self.assertEqual(completed.returncode, 1 if scenario == "web_forbidden" else 2)
                result = self.only(report)
                self.assertNotEqual(result["web"]["status"], "region_blocked")
                self.assertNotEqual(result["assessment"]["status"], "api_confirmed")

    def test_missing_web_endpoint_is_inconclusive(self):
        _, report = self.invoke("-4", scenario="web_missing")
        result = self.only(report)
        self.assertEqual(result["web"]["status"], "http_error")
        self.assertEqual(result["assessment"]["status"], "inconclusive")

    def test_unknown_html_is_not_an_app_success(self):
        _, report = self.invoke("-4", scenario="unrelated_html")
        result = self.only(report)
        self.assertEqual(result["web"]["status"], "unknown")
        self.assertEqual(result["assessment"]["status"], "inconclusive")

    def test_ipinfo_failure_does_not_prevent_other_checks(self):
        for scenario in ("ipinfo_failure", "ipinfo_html"):
            with self.subTest(scenario=scenario):
                _, report = self.invoke("-4", scenario=scenario, api=True)
                result = self.only(report)
                self.assertEqual(result["cloudflare"]["ip"], "203.0.113.10")
                self.assertEqual(result["api"]["status"], "working")

    def test_cloudflare_timeout_does_not_prevent_google_checks(self):
        _, report = self.invoke("-4", scenario="cloudflare_failure", api=True)
        result = self.only(report)
        self.assertEqual(result["api"]["status"], "working")
        self.assertEqual(result["ipinfo"]["ip"], "203.0.113.10")

    def test_cloudflare_crlf_is_parsed(self):
        _, report = self.invoke("-4", scenario="cloudflare_crlf")
        result = self.only(report)
        self.assertEqual(result["cloudflare"]["country"], "DE")
        self.assertEqual(result["cloudflare"]["warp"], "off")

    def test_egress_mismatch_produces_a_warning(self):
        _, report = self.invoke("-4", scenario="different_egress")
        result = self.only(report)
        self.assertNotEqual(result["ipinfo"]["ip"], result["cloudflare"]["ip"])
        self.assertTrue(result["warnings"])

    def test_default_checks_both_families(self):
        _, report = self.invoke()
        self.assertEqual({result["id"] for result in report["results"]}, {"ipv4", "ipv6"})
        families = {result["id"]: result for result in report["results"]}
        self.assertEqual(families["ipv4"]["ip"], "203.0.113.10")
        self.assertEqual(families["ipv6"]["ip"], "2001:db8::10")

    def test_ipv6_unavailable_does_not_invent_region_ban(self):
        _, report = self.invoke("--both", scenario="ipv6_unavailable")
        families = {result["id"]: result for result in report["results"]}
        self.assertEqual(families["ipv6"]["assessment"]["status"], "unreachable")
        self.assertEqual(families["ipv4"]["assessment"]["status"], "inconclusive")

    def test_translated_ipv6_does_not_confirm_the_requested_family(self):
        _, report = self.invoke("-6", scenario="ipv6_translated_to_ipv4", api=True)
        result = self.only(report)
        self.assertEqual(result["api"]["status"], "working")
        self.assertEqual(result["assessment"]["status"], "inconclusive")
        self.assertTrue(result["warnings"])

    def test_all_network_failure_is_unreachable(self):
        completed, report = self.invoke("-4", scenario="all_network")
        self.assertEqual(completed.returncode, 2)
        result = self.only(report)
        self.assertEqual(result["web"]["status"], "network_error")
        self.assertEqual(result["assessment"]["status"], "unreachable")

    def test_direct_mode_ignores_proxy_environment_and_curlrc(self):
        _, report = self.invoke("-4", environment={"HTTPS_PROXY": "http://bad-proxy.invalid:3128", "ALL_PROXY": "socks5://bad.invalid:1080", "NO_PROXY": "localhost"})
        self.assertEqual(self.only(report)["id"], "ipv4")
        for request in self.requests():
            args = request["argv"]
            self.assertEqual(args[0], "-q")
            self.assertEqual(args[args.index("--proxy") + 1], "")
            self.assertEqual(args[args.index("--noproxy") + 1], "*")
            self.assertIn("-4", args)

    def test_proxy_credentials_and_api_key_never_appear_in_argv_or_reports(self):
        completed, report = self.invoke("--proxy", TEST_PROXY, api=True, environment={"NO_PROXY": "*"})
        self.assertEqual(self.only(report)["id"], "proxy")
        for secret in (TEST_KEY, "PRIVATE-proxy-password"):
            self.assertNotIn(secret, completed.stdout)
            self.assertNotIn(secret, completed.stderr)
        for request in self.requests():
            argv = request["argv"]
            self.assertNotIn(TEST_KEY, " ".join(argv))
            self.assertNotIn("PRIVATE-proxy-password", " ".join(argv))
            self.assertNotIn("-4", argv)
            self.assertNotIn("-6", argv)
            self.assertIn(TEST_PROXY, request["config"])
            config = {}
            for line in request["config"].splitlines():
                if "=" in line:
                    name, value = line.split("=", 1)
                    parts = shlex.split(value.strip())
                    config[name.strip()] = parts[0] if parts else ""
            if "--noproxy" in argv:
                self.assertEqual(argv[argv.index("--noproxy") + 1], "")
            else:
                self.assertIn("noproxy", config)
                self.assertEqual(config["noproxy"], "")

    def test_reflected_api_key_is_redacted(self):
        completed, report = self.invoke("-4", scenario="reflected_key", api=True)
        self.assertNotIn(TEST_KEY, completed.stdout)
        self.assertNotIn(TEST_KEY, completed.stderr)
        self.assertNotEqual(self.only(report)["api"]["status"], "working")
        self.assertIn("[скрыто]", self.only(report)["api"]["detail"])
        self.assertTrue(any(TEST_KEY in request["config"] for request in self.generation_requests()))

    def test_api_key_file_is_used_without_printing_the_key(self):
        key_file = self.directory / "api-key.txt"
        key_file.write_text(TEST_KEY + "\n")
        key_file.chmod(0o600)
        completed, report = self.invoke("-4", "--key-file", str(key_file), environment={"GEMINI_KEY": "-"})
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.only(report)["api"]["status"], "working")
        self.assertTrue(any(TEST_KEY in request["config"] for request in self.generation_requests()))
        self.assertNotIn(TEST_KEY, completed.stdout + completed.stderr)

    def test_invalid_arguments_have_a_distinct_exit_code(self):
        for args in (("--timeout", "1"), ("--ipv4", "--ipv6"), ("--proxy", "not-a-proxy")):
            with self.subTest(args=args):
                completed, _ = self.invoke(*args, json_output=False)
                self.assertEqual(completed.returncode, 64)
        self.assertFalse(self.requests())

    def test_proxy_environment_is_supported(self):
        _, report = self.invoke(environment={"GEMINI_PROXY": TEST_PROXY})
        self.assertEqual(self.only(report)["id"], "proxy")
        self.assertTrue(all(TEST_PROXY in entry["config"] for entry in self.requests()))

    def test_legacy_positional_proxy_remains_supported(self):
        _, report = self.invoke(TEST_PROXY)
        self.assertEqual(self.only(report)["id"], "proxy")
        self.assertTrue(all(TEST_PROXY in entry["config"] for entry in self.requests()))

    def test_save_and_compare_report_changes(self):
        snapshot = self.directory / "baseline.json"
        _, report = self.invoke("-4", "--save", str(snapshot), api=True)
        self.assertTrue(snapshot.is_file())
        saved = json.loads(snapshot.read_text())
        self.assertEqual(saved["results"][0]["api"]["status"], "working")
        self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode) & 0o077, 0)
        self.assertNotIn(TEST_KEY, snapshot.read_text())
        _, changed = self.invoke("-4", "--compare", str(snapshot), scenario="api_region", api=True)
        self.assertTrue(changed["changes"])

    def test_unchanged_snapshot_has_no_changes(self):
        snapshot = self.directory / "baseline.json"
        self.invoke("-4", "--save", str(snapshot), api=True)
        _, report = self.invoke("-4", "--compare", str(snapshot), api=True)
        self.assertFalse(report["changes"])

    def test_snapshot_detects_model_alias_version_change(self):
        snapshot = self.directory / "baseline.json"
        self.invoke("-4", "--save", str(snapshot), api=True)
        _, report = self.invoke("-4", "--compare", str(snapshot), scenario="model_version_changed", api=True)
        self.assertTrue(any(change["field"] == "api.model_version" for change in report["changes"]))

    def test_snapshot_detects_different_selected_model(self):
        snapshot = self.directory / "baseline.json"
        self.invoke("-4", "--save", str(snapshot), "--model", "gemini-custom-lite", api=True)
        _, report = self.invoke("-4", "--compare", str(snapshot), "--model", "gemini-other-lite", api=True)
        self.assertTrue(any(change["field"] == "api.model" for change in report["changes"]))

    def test_api_requests_do_not_follow_redirects(self):
        self.invoke("-4", api=True)
        for request in self.requests():
            args = request["argv"]
            if "generativelanguage.googleapis.com/" in args[-1]:
                self.assertNotIn("-L", args)
                self.assertNotIn("--location", args)

    def test_no_api_option_overrides_environment_key(self):
        _, report = self.invoke("-4", "--no-api", api=True)
        self.assertEqual(self.only(report)["api"]["status"], "not_checked")
        self.assertFalse(any("generativelanguage.googleapis.com/" in entry["argv"][-1] for entry in self.requests()))


if __name__ == "__main__":
    unittest.main()
