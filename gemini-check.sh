#!/usr/bin/env bash
# One-file Gemini diagnostics. Browser mode adds optional Playwright + Chromium.
set -eu
for dependency in curl python3; do
  if ! command -v "$dependency" >/dev/null 2>&1; then
    printf 'Нужен %s. Debian/Ubuntu: apt install curl python3\n' "$dependency" >&2
    exit 2
  fi
done
gemini_python="python3"
gemini_browser_home="${GEMINI_BROWSER_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/gemini-check}"
for argument in "$@"; do
  case "$argument" in
    --browser|--install-browser)
      if [ -n "${GEMINI_BROWSER_PYTHON:-}" ]; then
        gemini_python="$GEMINI_BROWSER_PYTHON"
      elif [ -x "$gemini_browser_home/browser-venv/bin/python" ]; then
        gemini_python="$gemini_browser_home/browser-venv/bin/python"
      fi
      ;;
  esac
done
exec "$gemini_python" - "$@" <<'PYTHON_GEMINI_CHECK'
import argparse
import concurrent.futures
import datetime
import getpass
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

VERSION = "2.1"
BROWSER_PLAYWRIGHT_VERSION = "1.64.1"
API_BASE = "https://generativelanguage.googleapis.com/v1beta"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"
PROXY_ENV = ("http_proxy", "https_proxy", "all_proxy", "no_proxy",
             "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


def clean(value, secrets=(), limit=350):
    text = str(value or "")
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[скрыто]")
    text = re.sub(r"(?i)AIza[A-Za-z0-9_-]{20,}", "[ключ скрыт]", text)
    text = re.sub(r"(?i)(https?|socks5h?)://[^\s/@]+@", r"\1://[скрыто]@", text)
    return " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text).split())[:limit]


def valid_ip(value):
    if not isinstance(value, str):
        return ""
    try:
        return str(ipaddress.ip_address(value))
    except (ValueError, TypeError):
        return ""


def country(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z]{2}", value) else ""


def object_json(body):
    try:
        result = json.loads(body)
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def config_quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Curl:
    def __init__(self, directory, family, proxy, key, timeout):
        self.directory, self.family, self.proxy = directory, family, proxy
        self.key, self.timeout = key, timeout
        self.secrets = [key, proxy]
        if proxy:
            parsed = urlsplit(proxy)
            self.secrets += [unquote(s) for s in (parsed.username, parsed.password) if s and len(s) >= 4]
        self.env = {k: v for k, v in os.environ.items()
                    if k not in PROXY_ENV + ("GEMINI_KEY", "GEMINI_PROXY")}

    def request(self, url, api=False, payload=None, youtube=False):
        stem = Path(self.directory) / uuid.uuid4().hex
        body_file, header_file = str(stem) + ".body", str(stem) + ".headers"
        command = ["curl", "-q", "--config", "-", "--silent", "--show-error",
                   "--connect-timeout", str(min(5, self.timeout)),
                   "--max-time", str(self.timeout), "--max-redirs", "3",
                   "--proto", "=https", "--proto-redir", "=https", "--compressed",
                   "--max-filesize", "4194304", "--output", body_file,
                   "--dump-header", header_file,
                   "--write-out", "%{http_code}\n%{url_effective}\n%{remote_ip}\n%{time_total}"]
        settings = []
        if self.proxy:
            settings += ["proxy = " + config_quote(self.proxy), 'noproxy = ""']
        else:
            command += ["-" + self.family, "--proxy", "", "--noproxy", "*"]
        # API headers never follow a redirect to a different host.
        if api:
            settings.append("header = " + config_quote("x-goog-api-key: " + self.key))
        else:
            command += ["--location", "--user-agent", USER_AGENT,
                        "--header", "Accept-Language: en-US,en;q=0.9"]
        if youtube:
            command += ["--cookie", "SOCS=CAI"]
        if payload is not None:
            command += ["--header", "Content-Type: application/json",
                        "--data-binary", json.dumps(payload, separators=(",", ":"))]
        command.append(url)
        started = time.monotonic()
        try:
            process = subprocess.run(command, input="\n".join(settings) + "\n", env=self.env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     encoding="utf-8", errors="replace", timeout=self.timeout + 3)
            metadata = process.stdout.splitlines()
            http = int(metadata[0]) if metadata and metadata[0].isdigit() else 0
            effective = metadata[1] if len(metadata) > 1 else url
            remote = valid_ip(metadata[2]) if len(metadata) > 2 else ""
            code, error = process.returncode, clean(process.stderr, self.secrets)
        except subprocess.TimeoutExpired:
            http, effective, remote, code, error = 0, url, "", 28, "таймаут запроса"
        except OSError as exc:
            http, effective, remote, code, error = 0, url, "", 127, clean(exc, self.secrets)
        try:
            with open(body_file, "rb") as handle:
                body = handle.read(4194305).decode("utf-8", "replace")
        except OSError:
            body = ""
        for name in (body_file, header_file):
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass
        return {"http": http, "curl": code, "url": effective, "remote_ip": remote,
                "elapsed": round(time.monotonic() - started, 2), "error": error, "body": body}


def transport_detail(response):
    labels = {5: "не удалось разрешить адрес прокси", 6: "ошибка DNS",
              7: "нет соединения с сервером или прокси", 28: "таймаут",
              35: "ошибка TLS", 60: "не пройдена проверка TLS-сертификата",
              63: "слишком большой ответ", 97: "ошибка соединения SOCKS"}
    if response["curl"]:
        return labels.get(response["curl"], "ошибка curl %s" % response["curl"])
    return "HTTP %s" % response["http"]


def evidence(response):
    return {k: response[k] for k in ("http", "curl", "remote_ip", "elapsed")}


def probe_cloudflare(client):
    response = client.request("https://www.cloudflare.com/cdn-cgi/trace")
    fields = dict(line.split("=", 1) for line in response["body"].splitlines() if "=" in line)
    ip, loc = valid_ip(fields.get("ip")), country(fields.get("loc"))
    ok = response["curl"] == 0 and response["http"] == 200 and bool(ip and loc)
    return dict(evidence(response), status="ok" if ok else "unavailable", ip=ip if ok else "",
                country=loc if ok else "", colo=clean(fields.get("colo")) if ok else "",
                warp=clean(fields.get("warp")) if ok else "",
                detail="" if ok else transport_detail(response))


def probe_ipinfo(client):
    response = client.request("https://ipinfo.io/json")
    data = object_json(response["body"])
    ok = response["curl"] == 0 and response["http"] == 200 and bool(valid_ip(data.get("ip")))
    return dict(evidence(response), status="ok" if ok else "unavailable",
                ip=valid_ip(data.get("ip")) if ok else "", country=country(data.get("country")) if ok else "",
                org=clean(data.get("org"), client.secrets) if ok else "",
                detail="" if ok else transport_detail(response))


def probe_youtube(client):
    response = client.request("https://www.youtube.com/", youtube=True)
    matches = set(re.findall(r'"GL"\s*:\s*"([A-Z]{2})"', response["body"]))
    ok = (response["curl"] == 0 and response["http"] == 200 and len(matches) == 1
          and urlsplit(response["url"]).hostname in ("www.youtube.com", "youtube.com"))
    return dict(evidence(response), status="ok" if ok else "unavailable",
                country=matches.pop() if ok else "",
                detail="подсказка YouTube, не проверка Gemini" if ok else transport_detail(response))


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.parts = [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        hidden = (bool(self.stack and self.stack[-1][1])
                  or tag in ("script", "style", "template", "noscript")
                  or "hidden" in attrs or (attrs.get("aria-hidden") or "").lower() == "true"
                  or bool(re.search(r"display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse)",
                                    (attrs.get("style") or "").lower())))
        if tag not in ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"):
            self.stack.append((tag, hidden))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                self.stack = self.stack[:i]
                break

    def handle_data(self, text):
        if not self.stack or not self.stack[-1][1]:
            self.parts.append(text)


def probe_web(client):
    response = client.request("https://gemini.google.com/app?hl=en")
    result = dict(evidence(response), status="unknown", region_hint="", detail="")
    parsed = urlsplit(response["url"])
    # Do not publish redirect query strings or bootstrap/session tokens.
    result["endpoint"] = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    if response["curl"]:
        result.update(status="network_error", detail=transport_detail(response))
        return result
    parser = VisibleText()
    try:
        parser.feed(response["body"])
    except (ValueError, AssertionError):
        pass
    visible = " ".join(" ".join(parser.parts).casefold().replace("’", "'").split())
    region_gate = re.search(r"gemini (?:is |isn't |is not )?(?:currently )?(?:not |isn't )?"
                            r"(?:supported|available) in your (?:country|region|location)", visible)
    denied_text = region_gate and ("not " in region_gate.group() or "isn't " in region_gate.group())
    gate_url = (parsed.hostname in ("gemini.google.com", "gemini.google")
                and parse_qs(parsed.query).get("location") == ["unsupported"])
    if denied_text or gate_url:
        result.update(status="region_blocked", detail="страница Gemini явно сообщает об ограничении региона")
    elif ("our systems have detected unusual traffic" in visible
          or (parsed.hostname in ("www.google.com", "google.com") and parsed.path.startswith("/sorry"))
          or response["http"] == 429):
        result.update(status="challenge", detail="Google ограничил автоматический запрос или требует CAPTCHA; это не доказательство регионального бана")
    elif response["http"] >= 500:
        result.update(status="overloaded", detail=transport_detail(response) + ": временная ошибка сервиса")
    elif response["http"] != 200:
        result.update(status="http_error", detail=transport_detail(response) + ": причина отказа по IP не установлена")
    elif parsed.hostname == "accounts.google.com":
        result.update(status="login_required", detail="Google запросил вход в аккаунт; работа чата не проверена")
    elif parsed.hostname == "consent.google.com":
        result.update(status="consent", detail="страница согласия Google; работа чата не проверена")
    elif parsed.hostname == "gemini.google.com" and ("BardChatUi" in response["body"] or "WIZ_global_data" in response["body"]):
        result.update(status="page_available", detail="страница Gemini доступна; отправка сообщения в аккаунте не проверена")
    else:
        result["detail"] = "HTTP 200, но ответ не распознан как приложение Gemini; работа чата не подтверждена"
    if parsed.hostname == "gemini.google.com" and response["http"] == 200:
        hints = set(re.findall(r',2,1,200,"([A-Z]{3})"', response["body"]))
        if len(hints) == 1:
            result["region_hint"] = hints.pop()
    return result


def _browser_endpoint(url):
    """Only publish a site name; chat IDs, redirect queries and tokens stay local."""
    parsed = urlsplit(url)
    return "https://" + (parsed.hostname or "unknown")


def _browser_gate(page):
    parsed = urlsplit(page.url)
    host = parsed.hostname or ""
    try:
        visible = page.locator("body").inner_text(timeout=1000)
    except Exception:
        visible = ""
    visible = " ".join(visible.casefold().replace("’", "'").split())
    match = re.search(r"gemini (?:is |isn't |is not )?(?:currently )?(?:not |isn't )?"
                      r"(?:yet )?(?:supported|available) in your (?:country|region|location)", visible)
    negative = match and ("not " in match.group() or "isn't " in match.group())
    if negative or (host in ("gemini.google.com", "gemini.google")
                    and parse_qs(parsed.query).get("location") == ["unsupported"]):
        return "region_blocked", "сайт Gemini явно сообщил, что регион не поддерживается"
    if (host in ("www.google.com", "google.com") and parsed.path.startswith("/sorry")) or any(
            text in visible for text in ("our systems have detected unusual traffic", "verify you are human",
                                         "confirm you're not a robot", "unusual traffic from your computer network")):
        return "challenge", "Google запросил CAPTCHA или проверку; это не доказательство региональной блокировки IP"
    if host == "accounts.google.com":
        return "login_required", "для продолжения нужен вход в Google; анонимный тест не завершён"
    if host == "consent.google.com":
        return "consent", "нужно решить вопрос согласия Google; работа чата не проверена"
    for dialog in page.get_by_role("dialog").all():
        if dialog.is_visible():
            text = " ".join(dialog.inner_text(timeout=1000).casefold().split())
            if (any(x in text for x in ("sign in to chat", "sign in to use gemini", "sign in to continue", "sign in to gemini"))
                    or ("to continue this chat" in text and "sign in" in text)):
                return "login_required", "для продолжения сайт запросил вход в Google в диалоговом окне"
    if any(text in visible for text in ("you've reached your limit", "you have reached your limit",
                                       "you've reached the limit", "daily limit reached", "too many requests")):
        return "quota", "сайт сообщил о лимите запросов; это не доказательство региональной блокировки IP"
    return "", ""


def _browser_composer(page):
    selectors = ("rich-textarea [contenteditable='true'][role='textbox']",
                 ".ql-editor[contenteditable='true']", "textarea[aria-label]",
                 "[contenteditable='true'][role='textbox']")
    for selector in selectors:
        locator = page.locator(selector)
        for index in range(min(locator.count(), 8)):
            candidate = locator.nth(index)
            if candidate.is_visible() and candidate.is_editable():
                return candidate
    return None


def _browser_assistants(page):
    # Explicit model-response containers exclude the user's prompt and welcome text.
    selectors = ("model-response message-content", "model-response .model-response-text",
                 "[data-message-author-role='model'] message-content", "model-response",
                 ".model-response-text")
    values = {}
    for selector in selectors:
        locator = page.locator(selector)
        count = locator.count()
        for index in range(max(0, count - 20), count):
            candidate = locator.nth(index)
            if candidate.is_visible():
                values[(selector, index)] = candidate.inner_text(timeout=1000).strip()
    return values


def _browser_manual_resume(page, deadline):
    print("Gemini запросил вход или согласие. Выполните действие вручную в открытом окне.", file=sys.stderr)
    while time.monotonic() < deadline:
        gate = _browser_gate(page)
        if (not gate[0] and urlsplit(page.url).hostname == "gemini.google.com"
                and _browser_composer(page) is not None):
            # A new probe after the manual action avoids accepting an earlier reply.
            return test_browser_page(page, timeout=max(0.1, deadline - time.monotonic()), interactive=False)
        page.wait_for_timeout(250)
    return None


def test_browser_page(page, timeout=60, nonce=None, interactive=False):
    """Test the already-open Gemini UI. No navigation, cookies or raw RPC are exported."""
    token = nonce or "CHECK_" + uuid.uuid4().hex[:20]
    result = {"status": "unknown", "detail": "", "submitted": False,
              "response_verified": False, "request_seen": False, "nonce": token,
              "generation_http": 0}
    deadline = time.monotonic() + max(0.1, timeout)
    composer, last_gate = None, ("", "")
    try:
        while time.monotonic() < deadline:
            last_gate = _browser_gate(page)
            if last_gate[0]:
                if interactive and last_gate[0] in ("login_required", "consent"):
                    page.wait_for_timeout(250)
                    continue
                result.update(status=last_gate[0], detail=last_gate[1])
                return result
            if urlsplit(page.url).hostname != "gemini.google.com":
                result.update(status="unknown", detail="открыт другой сайт; тестовое сообщение не отправлено")
                return result
            composer = _browser_composer(page)
            if composer is not None:
                break
            page.wait_for_timeout(250)
        if composer is None:
            if last_gate[0]:
                result.update(status=last_gate[0], detail=last_gate[1])
                return result
            # A sign-in button alone is normal on an anonymous, usable chat.
            # Only an explicit prompt to sign in plus no composer is a login gate.
            body = " ".join(page.locator("body").inner_text(timeout=1000).casefold().split())
            if any(x in body for x in ("sign in to chat", "sign in to use gemini", "sign in to continue")):
                result.update(status="login_required", detail="сайт требует входа перед отправкой сообщения")
            else:
                result["detail"] = "не найден доступный редактор сообщения; интерфейс мог измениться или не загрузиться"
            return result

        before = _browser_assistants(page)
        if token in page.content() or any(token in text for text in before.values()):
            result["detail"] = "контрольный маркер уже был на странице до отправки; свежий ответ не подтверждён"
            return result
        prompt = "Reply with exactly %s. Do not add any other text." % token
        state = {"sending": False, "request_seen": False, "http": 0}

        def is_probe_request(request):
            if not state["sending"] or request.method != "POST":
                return False
            endpoint = urlsplit(request.url)
            # The current anonymous web UI serializes requests to this web backend.
            # Its private payload format is deliberately not decoded. A fresh model
            # reply with our unpredictable token remains mandatory for success.
            if endpoint.hostname == "geminiweb-pa.clients6.google.com" and endpoint.path == "/v1/processSession":
                return True
            if endpoint.hostname != "gemini.google.com":
                return False
            try:
                return token in (request.post_data or "")
            except Exception:
                return False

        def sent(request):
            if is_probe_request(request):
                state["request_seen"] = True

        def received(response):
            if is_probe_request(response.request):
                state["request_seen"] = True
                state["http"] = response.status

        page.on("request", sent)
        page.on("response", received)
        try:
            composer.fill(prompt, timeout=min(5000, max(100, int((deadline - time.monotonic()) * 1000))))
            state["sending"] = True
            button = None
            for selector in ("button[data-test-id='send-button']", "button[aria-label='Send message']",
                             "button[aria-label='Submit']", "button[aria-label='Send']"):
                candidates = page.locator(selector)
                for index in range(min(candidates.count(), 8)):
                    candidate = candidates.nth(index)
                    if candidate.is_visible() and candidate.is_enabled():
                        button = candidate
                        break
                if button is not None:
                    break
            remaining = min(5000, max(100, int((deadline - time.monotonic()) * 1000)))
            if button is not None:
                button.click(timeout=remaining)
            else:
                composer.press("Enter", timeout=remaining)
            result["submitted"] = True
            while time.monotonic() < deadline:
                result["request_seen"], result["generation_http"] = state["request_seen"], state["http"]
                gate = _browser_gate(page)
                if gate[0]:
                    if interactive and gate[0] in ("login_required", "consent"):
                        resumed = _browser_manual_resume(page, deadline)
                        if resumed is not None:
                            return resumed
                    result.update(status=gate[0], detail=gate[1])
                    return result
                if _browser_composer(page) is None:
                    visible = " ".join(page.locator("body").inner_text(timeout=1000).casefold().split())
                    if (any(x in visible for x in ("sign in to chat", "sign in to use gemini", "sign in to continue"))
                            or ("to continue this chat" in visible and "sign in" in visible)):
                        if interactive:
                            resumed = _browser_manual_resume(page, deadline)
                            if resumed is not None:
                                return resumed
                        result.update(status="login_required", detail="после отправки сайт потребовал входа в Google")
                        return result
                if state["http"] == 429:
                    result.update(status="quota", detail="запрос веб-чата получил HTTP 429; работа не подтверждена")
                    return result
                if state["http"] == 403:
                    result.update(status="access_denied", detail="запрос веб-чата получил HTTP 403; причина отказа по IP не установлена")
                    return result
                if state["http"] >= 500:
                    result.update(status="network_error", detail="Google вернул временную ошибку HTTP %s" % state["http"])
                    return result
                for identity, text in _browser_assistants(page).items():
                    if (state["request_seen"] and token in text and prompt not in text
                            and before.get(identity) != text):
                        result.update(status="working", response_verified=True,
                                      detail="после отправки сообщения получен новый ответ модели с контрольным маркером; Gemini WEB работает сейчас")
                        return result
                page.wait_for_timeout(250)
            result.update(status="timeout", detail=("новый ответ модели не получен за отведённое время"
                          if state["request_seen"] else "отправка сообщения на сервер Google не подтверждена; возможен сбой интерфейса"))
            return result
        finally:
            page.remove_listener("request", sent)
            page.remove_listener("response", received)
    except Exception:
        # A selector or automation failure is not an IP ban. Do not print raw errors.
        result.update(status="unknown", detail="браузерная проверка не завершена: интерфейс изменился, страница закрыта или действие не выполнено")
        return result


def _browser_proxy(proxy):
    if not proxy:
        return None
    parsed = urlsplit(proxy)
    scheme = parsed.scheme.lower()
    if scheme in ("socks5", "socks5h") and (parsed.username is not None or parsed.password is not None):
        raise ValueError("SOCKS5 с логином и паролем не поддерживается Chromium; используйте локальный SOCKS без авторизации или HTTP-прокси")
    if scheme not in ("http", "https", "socks5", "socks5h") or not parsed.hostname or not parsed.port:
        raise ValueError("неподдерживаемый адрес прокси браузера")
    host = parsed.hostname
    if ":" in host:
        host = "[" + host + "]"
    settings = {"server": "%s://%s:%s" % ("socks5" if scheme == "socks5h" else scheme, host, parsed.port)}
    if parsed.username is not None:
        settings["username"] = unquote(parsed.username)
        settings["password"] = unquote(parsed.password or "")
    return settings


def probe_browser(proxy="", timeout=60, headed=False, profile=None):
    started = time.monotonic()
    deadline = started + timeout
    result = {"status": "unknown", "detail": "", "ip": "", "country": "", "colo": "", "warp": "",
              "submitted": False, "response_verified": False, "request_seen": False,
              "http": 0, "endpoint": "https://gemini.google.com", "elapsed": 0}
    try:
        settings = _browser_proxy(proxy)
    except (ValueError, TypeError):
        result.update(status="proxy_unsupported", detail="Chromium не поддерживает такой прокси, включая SOCKS5 с авторизацией; используйте локальный SOCKS без пароля или HTTP-прокси")
        return result
    try:
        from playwright.sync_api import sync_playwright, Error as BrowserError, TimeoutError as BrowserTimeout
    except ImportError:
        result.update(status="dependency_missing", detail="Playwright не установлен. Выполните gemini-check --install-browser")
        return result
    environment = {k: v for k, v in os.environ.items()
                   if k not in PROXY_ENV + ("GEMINI_KEY", "GEMINI_PROXY")}
    browser, context = None, None
    try:
        with sync_playwright() as playwright:
            launch = {"headless": not headed, "env": environment,
                      "args": [] if settings else ["--no-proxy-server"],
                      "timeout": min(30000, max(1000, int(timeout * 1000)))}
            if settings:
                launch["proxy"] = settings
            try:
                if profile:
                    context = playwright.chromium.launch_persistent_context(str(profile), locale="en-US", accept_downloads=False, **launch)
                else:
                    browser = playwright.chromium.launch(**launch)
                    context = browser.new_context(locale="en-US", accept_downloads=False)
            except BrowserError as exc:
                text = str(exc).casefold()
                missing = "executable doesn't exist" in text or "playwright install" in text
                result.update(status="dependency_missing" if missing else "launch_error",
                              detail="не найден Chromium; выполните gemini-check --install-browser" if missing else "Chromium не запустился; проверьте системные библиотеки и доступность графики для --headed")
                return result
            try:
                trace = context.new_page()
                try:
                    response = trace.goto("https://www.cloudflare.com/cdn-cgi/trace", wait_until="domcontentloaded",
                                          timeout=max(500, int(min(10, timeout / 4) * 1000)))
                    if response and response.status == 200:
                        text = trace.locator("body").inner_text(timeout=1000)
                        fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
                        result.update(ip=valid_ip(fields.get("ip")), country=country(fields.get("loc")),
                                      colo=clean(fields.get("colo")), warp=clean(fields.get("warp")))
                except BrowserError:
                    pass
                finally:
                    trace.close()
                page = context.new_page()
                try:
                    response = page.goto("https://gemini.google.com/app?hl=en", wait_until="domcontentloaded",
                                         timeout=max(500, int(min(30, max(0, deadline - time.monotonic())) * 1000)))
                    result["http"] = response.status if response else 0
                    result["endpoint"] = _browser_endpoint(page.url)
                    try:
                        page.wait_for_load_state("networkidle", timeout=max(100, int(min(3, max(0, deadline - time.monotonic())) * 1000)))
                    except BrowserTimeout:
                        pass
                    if headed and profile:
                        print("Окно Gemini открыто. Если требуется, войдите вручную; проверка продолжится до истечения таймаута.", file=sys.stderr)
                    outcome = test_browser_page(page, timeout=max(0.1, deadline - time.monotonic()), interactive=bool(headed and profile))
                    result.update(outcome)
                    result["endpoint"] = _browser_endpoint(page.url)
                    if result["status"] == "unknown" and result["http"] == 403:
                        result.update(status="access_denied", detail="сайт Gemini ответил HTTP 403; причина отказа не установлена")
                    return result
                except BrowserTimeout:
                    result.update(status="timeout", detail="страница Gemini не загрузилась за отведённое время; это не доказательство блокировки IP")
                    return result
                except BrowserError as exc:
                    text = str(exc)
                    note = "ошибка DNS" if "ERR_NAME_NOT_RESOLVED" in text else (
                        "ошибка TLS-сертификата" if "ERR_CERT" in text else (
                            "не удалось подключиться к прокси" if "ERR_PROXY" in text or "ERR_SOCKS" in text else "ошибка соединения браузера"))
                    result.update(status="network_error", detail=note)
                    return result
            finally:
                if context is not None:
                    try:
                        context.close()
                    except BrowserError:
                        pass
                if browser is not None:
                    try:
                        browser.close()
                    except BrowserError:
                        pass
    except BrowserError:
        result.update(status="launch_error", detail="браузерная сессия неожиданно завершилась; проверьте Chromium и системные зависимости")
        return result
    finally:
        result["elapsed"] = round(time.monotonic() - started, 2)


def api_error(response, client):
    if response["curl"]:
        return {"status": "network_error", "detail": transport_detail(response)}
    data = object_json(response["body"])
    error = data.get("error")
    if not isinstance(error, dict):
        status = "quota" if response["http"] == 429 else ("overloaded" if response["http"] >= 500 else "invalid_response")
        return {"status": status, "detail": transport_detail(response) + ": нет ожидаемого JSON API"}
    message = clean(error.get("message"), client.secrets)
    text = message.casefold()
    reasons = [item["reason"] for item in error.get("details", [])
               if isinstance(item, dict) and isinstance(item.get("reason"), str)] if isinstance(error.get("details"), list) else []
    reason_text = " ".join(reasons).casefold()
    # Quota messages also mention billing; that does not make them billing errors.
    if response["http"] == 429 or error.get("status") == "RESOURCE_EXHAUSTED":
        status = "quota"
    elif response["http"] >= 500:
        status = "overloaded"
    # Country-limited free tier / account verification is not an IP-region ban.
    elif "free tier" in text or "billing" in text or "billing" in reason_text or "free_tier" in reason_text:
        status = "billing"
    elif ("api_key" in reason_text or "key_expired" in reason_text
          or ("api key" in text and any(x in text for x in ("invalid", "expired", "leaked", "blocked", "not valid")))):
        status = "key_error"
    elif (re.search(r"(?:user )?location (?:is )?not supported", text)
          or re.search(r"(?:this api|gemini|the api) is not available in your (?:current )?(?:location|country|region)", text)
          or "unsupported_country" in reason_text or "region_not_supported" in reason_text):
        status = "region_blocked"
    elif response["http"] in (401, 403):
        status = "permission_denied"
    elif response["http"] == 404:
        status = "model_missing"
    else:
        status = "api_error"
    return {"status": status, "detail": message or transport_detail(response),
            "google_status": clean(error.get("status"), client.secrets), "reasons": [clean(x, client.secrets) for x in reasons]}


def model_candidates(data):
    models = data.get("models", [])
    if not isinstance(models, list):
        return []
    names = []
    for item in models:
        if not isinstance(item, dict):
            continue
        name, methods = item.get("name", ""), item.get("supportedGenerationMethods", [])
        if (not isinstance(name, str) or not re.fullmatch(r"models/gemini-[A-Za-z0-9._-]+", name)
                or not isinstance(methods, list) or "generateContent" not in methods):
            continue
        if "flash" not in name or re.search(r"tts|image|live|audio|embed|customtools|robotics", name):
            continue
        names.append(name)
    def rank(name):
        numbers = tuple(-int(x) for x in re.findall(r"\d+", name)[:3])
        return (0 if "flash-lite" in name else 1, 1 if re.search(r"preview|exp", name) else 0,
                0 if "latest" in name else 1, numbers, name)
    return sorted(set(names), key=rank)[:2]


def probe_api(client, model):
    result = {"status": "not_checked", "detail": "ключ API не задан", "attempts": []}
    if not client.key:
        return result
    if model:
        names = ["models/" + model]
    else:
        response = client.request(API_BASE + "/models?pageSize=1000", api=True)
        data = object_json(response["body"])
        result["discovery"] = evidence(response)
        if response["curl"] or response["http"] != 200 or "error" in data:
            result.update(api_error(response, client))
            return result
        if not isinstance(data.get("models"), list):
            result.update(status="invalid_response", detail="список моделей не содержит ожидаемый JSON models")
            return result
        names = model_candidates(data)
        if not names:
            result.update(status="no_model", detail="не найдена текстовая Flash-модель с generateContent; задайте --model")
            return result
    payload = {"contents": [{"parts": [{"text": "Reply only OK."}]}],
               "generationConfig": {"maxOutputTokens": 64}}
    for name in names:
        response = client.request(API_BASE + "/" + name + ":generateContent", api=True, payload=payload)
        data = object_json(response["body"])
        answer = False
        if not response["curl"] and response["http"] == 200 and "error" not in data:
            for candidate in data.get("candidates", []) if isinstance(data.get("candidates"), list) else []:
                content = candidate.get("content", {}) if isinstance(candidate, dict) else {}
                parts = content.get("parts", []) if isinstance(content, dict) else []
                if isinstance(parts, list):
                    answer |= any(isinstance(p, dict) and isinstance(p.get("text"), str)
                                  and bool(p["text"].strip()) for p in parts)
        if answer:
            state = {"status": "working", "detail": "модель вернула текст; доступ к API подтверждён на момент проверки"}
        elif not response["curl"] and response["http"] == 200 and "error" not in data:
            state = {"status": "invalid_response", "detail": "API вернул HTTP 200 без текста модели; генерация не подтверждена"}
        else:
            state = api_error(response, client)
        result["attempts"].append(dict(evidence(response), model=name, **state))
        result.pop("google_status", None)
        result.pop("reasons", None)
        result.update(state)
        result["model"] = name
        result["model_version"] = clean(data.get("modelVersion"), client.secrets)
        if state["status"] not in ("model_missing", "overloaded"):
            break
    return result


def assess(result):
    web, api = result["web"]["status"], result["api"]["status"]
    web_restricted = web in ("region_blocked", "challenge") or (web == "http_error" and result["web"]["http"] == 403)
    if api == "working" and web_restricted:
        return {"status": "mixed", "detail": "API работает, но на WEB получено ограничение; результаты сервисов различаются"}
    if api == "region_blocked" or web == "region_blocked":
        return {"status": "restricted", "detail": "получен явный отказ по региону; смотрите отдельно WEB и API"}
    if result.get("family_mismatch"):
        return {"status": "inconclusive", "detail": "внешние сервисы видят IP другого семейства; проверьте трансляцию и повторите проверку нужного выхода"}
    if api == "working":
        return {"status": "api_confirmed", "detail": "API РАБОТАЕТ на этом маршруте; работа WEB в аккаунте не подтверждена"}
    if web_restricted:
        return {"status": "restricted", "detail": "запрос к WEB ограничен; это не устанавливает причину и не доказывает региональный бан"}
    if (web == "network_error" and not result["ip"]
            and result["youtube"]["curl"] and api in ("not_checked", "network_error")):
        return {"status": "unreachable", "detail": "НЕТ СОЕДИНЕНИЯ на этом маршруте; это не признак блокировки Gemini"}
    return {"status": "inconclusive", "detail": "ДОСТУП НЕ ПОДТВЕРЖДЁН; страница и геобазы не заменяют отправку сообщения"}


def check_route(directory, route_id, proxy, key, timeout, model):
    family = {"ipv4": "4", "ipv6": "6", "proxy": ""}[route_id]
    client = Curl(directory, family, proxy, key, timeout)
    probes = {"cloudflare": probe_cloudflare, "ipinfo": probe_ipinfo,
              "youtube": probe_youtube, "web": probe_web, "api": lambda c: probe_api(c, model)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        pending = {name: pool.submit(function, client) for name, function in probes.items()}
        result = {name: future.result() for name, future in pending.items()}
    result.update(id=route_id, ip=result["cloudflare"]["ip"] or result["ipinfo"]["ip"], warnings=[])
    cf, info, yt = result["cloudflare"], result["ipinfo"], result["youtube"]
    if cf["ip"] and info["ip"] and cf["ip"] != info["ip"]:
        result["warnings"].append("Cloudflare и ipinfo видят разные IP: возможен разный выход по доменам или ротация прокси")
    if result["ip"] and family and ipaddress.ip_address(result["ip"]).version != int(family):
        result["family_mismatch"] = True
        result["warnings"].append("Выходной IP не совпадает с запрошенным семейством; проверьте трансляцию и маршрутизацию")
    if len({x["country"] for x in (cf, info, yt) if x["country"]}) > 1:
        result["warnings"].append("Страны в геобазах и YouTube различаются; ни одна из них не подтверждает доступ Gemini")
    if result["api"]["status"] == "working" and result["web"]["status"] != "page_available":
        result["warnings"].append("Ответ API не подтверждает работу сайта Gemini: у них отдельные ограничения")
    result["assessment"] = assess(result)
    return result


def compare_reports(previous, current):
    if not isinstance(previous, dict) or previous.get("schema_version") != 1 or not isinstance(previous.get("results"), list):
        raise ValueError("несовместимый формат предыдущего отчёта")
    old = {r["id"]: r for r in previous["results"] if isinstance(r, dict) and isinstance(r.get("id"), str)}
    changes = []
    paths = (("ip",), ("cloudflare", "country"), ("ipinfo", "country"),
             ("youtube", "country"), ("web", "region_hint"), ("web", "status"),
             ("api", "status"), ("api", "model"), ("api", "model_version"), ("assessment", "status"))
    def value(record, path):
        for key in path:
            record = record.get(key, "") if isinstance(record, dict) else ""
        return clean(record)
    for result in current["results"]:
        before = old.get(result["id"])
        if before is None:
            changes.append({"route": result["id"], "field": "route", "before": "", "after": "новый маршрут"})
            continue
        for path in paths:
            a, b = value(before, path), value(result, path)
            if a != b:
                changes.append({"route": result["id"], "field": ".".join(path), "before": a, "after": b})
    return changes


def check_browser_route(args):
    profile = args.browser_profile
    if profile:
        path = Path(profile).expanduser().resolve()
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
        (path / ".gemini-check-profile").touch(mode=0o600, exist_ok=True)
        profile = str(path)
    browser = probe_browser(proxy=args.proxy, timeout=args.browser_timeout,
                            headed=args.headed, profile=profile)
    status = browser["status"]
    if status == "working" and browser.get("response_verified") and browser.get("submitted"):
        assessment = {"status": "web_confirmed", "detail": "GEMINI WEB РАБОТАЕТ: получен новый ответ на тестовое сообщение через браузер"}
    elif status in ("region_blocked", "access_denied"):
        assessment = {"status": "restricted", "detail": "сайт Gemini отказал в доступе на этом маршруте; точная причина указана в результате браузера"}
    else:
        assessment = {"status": "inconclusive", "detail": "РАБОТА ЧАТА НЕ ПОДТВЕРЖДЕНА; этот результат сам по себе не означает блокировку IP"}
    blank = {"http": 0, "curl": 0, "remote_ip": "", "elapsed": 0,
             "status": "not_checked", "detail": "в браузерном режиме не используется"}
    cf = dict(blank, status="ok" if browser.get("ip") else "unavailable",
              http=200 if browser.get("ip") else 0,
              detail="IP определён через Chromium" if browser.get("ip") else "браузер не получил IP от Cloudflare",
              ip=browser.get("ip", ""), country=browser.get("country", ""),
              colo=browser.get("colo", ""), warp=browser.get("warp", ""))
    result = {"id": "browser", "ip": browser.get("ip", ""), "browser": browser,
              "cloudflare": cf, "ipinfo": dict(blank, ip="", country="", org=""),
              "youtube": dict(blank, country=""),
              "web": dict(blank, status=status, http=browser.get("http", 0), region_hint="", detail=browser["detail"]),
              "api": {"status": "not_checked", "detail": "браузерный режим работает без API", "attempts": []},
              "assessment": assessment, "warnings": []}
    if not result["ip"]:
        result["warnings"].append("внешний IP в браузере не определён; результат относится к маршруту, а не к подтверждённому адресу")
    result["warnings"].append("Браузер выбирает IPv4/IPv6 сам; запросы curl не используются для определения его выхода")
    return result


def browser_home():
    configured = os.environ.get("GEMINI_BROWSER_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path(os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")) / "gemini-check"


def install_browser():
    destination = browser_home() / "browser-venv"
    executable = destination / "bin" / "python"
    print("Установка Playwright %s и Chromium в %s" % (BROWSER_PLAYWRIGHT_VERSION, destination), flush=True)
    setup_env = {k: v for k, v in os.environ.items() if k not in ("GEMINI_KEY", "GEMINI_PROXY")}
    try:
        if not executable.is_file():
            candidates = [os.environ.get("GEMINI_BROWSER_PYTHON", ""), sys.executable]
            candidates += [shutil.which("python3.%s" % v) for v in range(14, 9, -1)]
            selected = None
            for candidate in candidates:
                if candidate and subprocess.run([candidate, "-c", "import sys;sys.exit(sys.version_info < (3,10))"],
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=setup_env).returncode == 0:
                    selected = candidate
                    break
            if not selected:
                print("Для браузера нужен Python 3.10+. Укажите его через GEMINI_BROWSER_PYTHON=/путь/к/python3.", file=sys.stderr)
                return 2
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            subprocess.run([selected, "-m", "venv", str(destination)], check=True, env=setup_env)
        subprocess.run([str(executable), "-c", "import sys;sys.exit(sys.version_info < (3,10))"], check=True, env=setup_env)
        subprocess.run([str(executable), "-m", "ensurepip", "--upgrade"], check=True, env=setup_env)
        subprocess.run([str(executable), "-m", "pip", "install", "--disable-pip-version-check",
                        "playwright==" + BROWSER_PLAYWRIGHT_VERSION], check=True, env=setup_env)
        subprocess.run([str(executable), "-m", "playwright", "install", "chromium"], check=True, env=setup_env)
    except (OSError, subprocess.CalledProcessError):
        print("Установка не завершена. Нужны Python 3.10+, модуль venv и доступ к PyPI/CDN Playwright.", file=sys.stderr)
        print("Debian/Ubuntu: apt install python3-venv", file=sys.stderr)
        return 2
    print("Готово. Запуск: gemini-check --browser")
    if sys.platform.startswith("linux"):
        print("Если Chromium сообщит о недостающих системных библиотеках, выполните с нужными правами:")
        print("  %s -m playwright install-deps chromium" % config_quote(str(executable)))
    return 0


API_LABELS = {"working": "РАБОТАЕТ", "not_checked": "НЕ ПРОВЕРЕНО", "region_blocked": "ОТКАЗ ПО РЕГИОНУ",
              "key_error": "ПРОБЛЕМА КЛЮЧА", "billing": "БИЛЛИНГ / ДОСТУП К FREE TIER",
              "permission_denied": "НЕТ ПРАВ КЛЮЧА ИЛИ ПРОЕКТА", "quota": "КВОТА / ЛИМИТ ЗАПРОСОВ",
              "overloaded": "ВРЕМЕННАЯ ОШИБКА GOOGLE", "network_error": "ОШИБКА СЕТИ",
              "invalid_response": "ГЕНЕРАЦИЯ НЕ ПОДТВЕРЖДЕНА", "no_model": "НЕТ ПОДХОДЯЩЕЙ МОДЕЛИ",
              "model_missing": "МОДЕЛЬ НЕДОСТУПНА", "api_error": "ОШИБКА API"}

BROWSER_LABELS = {"working": "ОТВЕЧАЕТ", "region_blocked": "ОТКАЗ ПО РЕГИОНУ",
                  "login_required": "ТРЕБУЕТСЯ ВХОД", "challenge": "ПРОВЕРКА GOOGLE / CAPTCHA",
                  "consent": "ТРЕБУЕТСЯ СОГЛАСИЕ", "quota": "ЛИМИТ ЗАПРОСОВ", "timeout": "ТАЙМАУТ",
                  "network_error": "ОШИБКА СЕТИ", "dependency_missing": "НУЖНА УСТАНОВКА БРАУЗЕРА",
                  "launch_error": "БРАУЗЕР НЕ ЗАПУСТИЛСЯ", "proxy_unsupported": "ПРОКСИ НЕ ПОДДЕРЖИВАЕТСЯ",
                  "access_denied": "ДОСТУП ОТКЛОНЁН", "unknown": "НЕ ОПРЕДЕЛЕНО"}


def print_report(report):
    print("gemini-check %s | %s" % (VERSION, report["checked_at"]))
    browser_mode = any(item["id"] == "browser" for item in report["results"])
    print("Проверка обычного веб-чата через Chromium, без API." if browser_mode else "Проверка IP и маршрута. WEB и API оцениваются отдельно.")
    for item in report["results"]:
        if item["id"] == "browser":
            b = item["browser"]
            print("\nChromium | внешний IP в браузере: %s" % (item["ip"] or "не определён"))
            print("  Cloudflare:   страна %s | узел %s | WARP %s" % (b.get("country") or "?", b.get("colo") or "?", b.get("warp") or "?"))
            print("  Gemini WEB:   %s: %s" % (BROWSER_LABELS.get(b["status"], b["status"]), b["detail"]))
            print("  Сообщение:    %s | новый ответ проверен: %s" % ("отправлено" if b.get("submitted") else "не отправлено", "да" if b.get("response_verified") else "нет"))
            print("  ОЦЕНКА:       %s" % item["assessment"]["detail"])
            for warning in item["warnings"]:
                print("  Примечание:   " + warning)
            continue
        print("\n%s | выходной IP: %s" % ({"ipv4": "IPv4 сервера", "ipv6": "IPv6 сервера", "proxy": "Прокси"}[item["id"]], item["ip"] or "не определён"))
        cf, info, yt = item["cloudflare"], item["ipinfo"], item["youtube"]
        print("  ipinfo:       %s | %s | %s" % (info["ip"] or "?", info["country"] or "?", info["org"] or info["detail"]))
        if cf["status"] == "ok":
            print("  Cloudflare:   %s | страна %s | узел %s | WARP %s" % (cf["ip"], cf["country"], cf["colo"] or "?", cf["warp"] or "?"))
        else:
            print("  Cloudflare:   не удалось проверить (%s)" % cf["detail"])
        print("  YouTube:      %s (косвенный признак)" % (yt["country"] or "не определено"))
        print("  Gemini WEB:   %s" % item["web"]["detail"])
        if item["web"]["region_hint"]:
            print("  Регион Gemini: %s (эвристика внутренних данных страницы)" % item["web"]["region_hint"])
        print("  Gemini API:   %s: %s" % (API_LABELS[item["api"]["status"]], item["api"]["detail"]))
        for attempt in item["api"]["attempts"]:
            print("    %s: HTTP %s, %s" % (attempt["model"].split("/")[-1], attempt["http"], API_LABELS[attempt["status"]]))
        print("  ОЦЕНКА:       %s" % item["assessment"]["detail"])
        for warning in item["warnings"]:
            print("  Примечание:   " + warning)
    if "compared_at" in report:
        print("\nСравнение с отчётом: " + report["compared_at"])
        if not report["changes"]:
            print("  Проверяемые IP, регионы и статусы не изменились.")
        for change in report["changes"]:
            print("  %s %s: %s -> %s" % (change["route"], change["field"], change["before"] or "?", change["after"] or "?"))
        if any(c["field"] == "ip" for c in report["changes"]):
            print("  Выходной IP изменился: это не сравнение репутации одного и того же IP.")
    if len(report["results"]) > 1 and len({r["assessment"]["status"] for r in report["results"]}) > 1:
        print("\nIPv4 и IPv6 дали разные результаты. Проверьте, какой маршрут используют клиенты VPN.")
    print("\nПроверяйте тот же выход и маршрутизацию доменов Google, что используют клиенты VPN.")
    if browser_mode:
        print("Успех подтверждает работу чата сейчас для проверенного браузера. Для других аккаунтов условия могут отличаться.")
    else:
        print("Закрытую репутацию IP и будущую доступность узнать нельзя. Для WEB нужна отправка сообщения через браузер.")


class Arguments(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(64, "Ошибка аргументов: %s\n" % message)


def arguments():
    parser = Arguments(prog="gemini-check", description="Проверка маршрута сервера для Gemini WEB и API. Требуются curl и Python 3.8+.")
    parser.add_argument("proxy_address", nargs="?", help="HTTP/SOCKS5 прокси (прежний формат запуска)")
    parser.add_argument("--proxy", help="прокси; безопаснее задать GEMINI_PROXY через окружение")
    family = parser.add_mutually_exclusive_group()
    family.add_argument("-4", "--ipv4", action="store_const", const="ipv4", dest="family", help="только IPv4 сервера")
    family.add_argument("-6", "--ipv6", action="store_const", const="ipv6", dest="family", help="только IPv6 сервера")
    family.add_argument("--both", action="store_const", const="both", dest="family", help="IPv4 и IPv6 отдельно (по умолчанию)")
    parser.add_argument("--no-api", action="store_true", help="без ключа и без обращения к API")
    parser.add_argument("--browser", action="store_true", help="настоящий тест веб-чата в Chromium без API-ключа")
    parser.add_argument("--install-browser", action="store_true", help="установить отдельное окружение Playwright и Chromium")
    parser.add_argument("--browser-timeout", type=int, default=60, help="лимит браузерной проверки, 15..300 секунд (60)")
    parser.add_argument("--headed", action="store_true", help="показать окно Chromium (нужен --browser и графический экран)")
    parser.add_argument("--browser-profile", metavar="DIR", help="отдельный профиль для ручного Google-входа; нужен --browser")
    parser.add_argument("--key-file", help="прочитать ключ API из локального файла")
    parser.add_argument("--model", default="", help="модель для generateContent вместо автоматического выбора")
    parser.add_argument("--timeout", type=int, default=12, help="лимит секунд на HTTP-запрос, от 5 до 60 (12)")
    parser.add_argument("--json", action="store_true", help="вывести только JSON; ключ не запрашивается")
    parser.add_argument("--save", metavar="FILE", help="сохранить JSON-снимок без ключа и адреса прокси")
    parser.add_argument("--compare", metavar="FILE", help="сравнить с предыдущим JSON-снимком")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args()
    if not 5 <= args.timeout <= 60:
        parser.error("--timeout должен быть от 5 до 60")
    if not 15 <= args.browser_timeout <= 300:
        parser.error("--browser-timeout должен быть от 15 до 300")
    if args.install_browser and (args.browser or args.family or args.headed or args.browser_profile or args.json or args.save or args.compare or args.proxy or args.proxy_address):
        parser.error("--install-browser запускается отдельно от проверки")
    if args.browser and args.family:
        parser.error("браузер выбирает семейство сам; не совмещайте --browser с -4/-6/--both")
    if not args.browser and (args.headed or args.browser_profile):
        parser.error("--headed и --browser-profile требуют --browser")
    if args.browser and (args.key_file or args.model):
        parser.error("--browser работает без API: --key-file и --model здесь не используются")
    if args.browser_profile:
        path = Path(args.browser_profile).expanduser()
        try:
            if path.exists() and (not path.is_dir() or (any(path.iterdir()) and not (path / ".gemini-check-profile").is_file())):
                parser.error("--browser-profile должен указывать на пустую папку или профиль, созданный gemini-check")
        except OSError:
            parser.error("не удалось проверить папку --browser-profile")
    if args.proxy and args.proxy_address:
        parser.error("укажите прокси только одним способом")
    args.proxy = args.proxy or args.proxy_address or os.environ.get("GEMINI_PROXY", "")
    if args.install_browser:
        return args, ""
    if args.proxy:
        try:
            parsed = urlsplit(args.proxy)
            valid = (parsed.scheme in ("http", "https", "socks5", "socks5h") and parsed.hostname
                     and parsed.port and parsed.path in ("", "/") and not parsed.query and not parsed.fragment
                     and not re.search(r"[\x00-\x20\x7f]", args.proxy))
        except ValueError:
            valid = False
        if not valid:
            parser.error("прокси должен иметь вид socks5h://[логин:пароль@]хост:порт или http(s)://хост:порт")
        if args.family:
            parser.error("с прокси не задавайте -4/-6/--both: семейство назначения выбирает прокси")
    if args.model:
        args.model = args.model[7:] if args.model.startswith("models/") else args.model
        if not re.fullmatch(r"gemini-[A-Za-z0-9._-]+", args.model):
            parser.error("--model должен быть идентификатором gemini-...")
    if args.browser:
        return args, ""
    key = os.environ.get("GEMINI_KEY", "")
    skip = args.no_api or (key == "-" and not args.key_file)
    if args.key_file and not skip:
        try:
            key = Path(args.key_file).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            parser.error("не удалось прочитать --key-file")
    if skip:
        key = ""
    elif not key and not args.proxy and not args.json:
        try:
            with open("/dev/tty", "r+") as terminal:
                print("Ключ Gemini API для проверки генерации (Enter: только предварительная WEB-проверка).", file=terminal)
                key = getpass.getpass("> ", stream=terminal).strip()
        except (OSError, EOFError):
            key = ""
    if key and (len(key) > 512 or re.search(r"[\x00-\x20\x7f]", key)):
        parser.error("некорректный формат ключа API")
    return args, key


def save_report(path, content):
    destination = Path(path).expanduser()
    fd, temporary = tempfile.mkstemp(prefix=".gemini-check-", dir=str(destination.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content + "\n")
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    if sys.version_info < (3, 8):
        print("Нужен Python 3.8 или новее.", file=sys.stderr)
        return 2
    args, key = arguments()
    if args.install_browser:
        return install_browser()
    previous = None
    if args.compare:
        try:
            with open(Path(args.compare).expanduser(), "r", encoding="utf-8") as handle:
                previous = json.loads(handle.read(2097153))
            compare_reports(previous, {"results": []})
        except (OSError, UnicodeError, ValueError):
            print("Не удалось прочитать совместимый JSON-отчёт --compare.", file=sys.stderr)
            return 2
    if args.browser:
        if not args.json:
            print("Проверяю обычный Gemini через Chromium. Лимит: %s с.\n" % args.browser_timeout, flush=True)
        try:
            results = [check_browser_route(args)]
        except OSError:
            print("Не удалось открыть отдельную папку профиля браузера.", file=sys.stderr)
            return 2
    else:
        routes = ["proxy"] if args.proxy else ([args.family] if args.family in ("ipv4", "ipv6") else ["ipv4", "ipv6"])
        if not args.json:
            print("Проверяю %s. Лимит на запрос: %s с.\n" % (", ".join(routes), args.timeout), flush=True)
        with tempfile.TemporaryDirectory(prefix="gemini-check-") as directory:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(routes)) as pool:
                pending = [pool.submit(check_route, directory, route, args.proxy, key, args.timeout, args.model) for route in routes]
                results = [future.result() for future in pending]
    report = {"schema_version": 1, "version": VERSION,
              "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
              "results": results, "changes": []}
    if previous is not None:
        report["compared_at"] = clean(previous.get("checked_at"))
        report["changes"] = compare_reports(previous, report)
    content = json.dumps(report, ensure_ascii=False, indent=2)
    if args.save:
        try:
            save_report(args.save, content)
        except OSError:
            print("Не удалось сохранить --save. Проверьте путь и права доступа.", file=sys.stderr)
            return 2
    if args.json:
        print(content)
    else:
        print_report(report)
    states = {r["assessment"]["status"] for r in results}
    if states.intersection(("restricted", "mixed")):
        return 1
    if states.intersection(("api_confirmed", "web_confirmed")) and states <= {"api_confirmed", "web_confirmed", "unreachable"}:
        return 0
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nПроверка прервана.", file=sys.stderr)
        sys.exit(130)
PYTHON_GEMINI_CHECK
