#!/usr/bin/env bash
# One-file Gemini diagnostics. --auto installs prerequisites and tests the web chat.
set -eu
gemini_python="python3"
gemini_browser_home="${GEMINI_BROWSER_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/gemini-check}"
gemini_auto=0
gemini_browser=0
gemini_help=0
for argument in "$@"; do
  case "$argument" in
    --auto) gemini_auto=1; gemini_browser=1 ;;
    --help|-h|--version) gemini_help=1 ;;
    --browser|--install-browser)
      gemini_browser=1
      ;;
  esac
done

gemini_python_works() {
  command -v "$1" >/dev/null 2>&1 && "$1" -c "import sys;sys.exit(sys.version_info < (3, $2))" >/dev/null 2>&1
}

gemini_find_python() {
  local candidate
  for candidate in "${GEMINI_BROWSER_PYTHON:-}" "$gemini_browser_home/browser-venv/bin/python" /usr/bin/python3 python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
    if [ -n "$candidate" ] && gemini_python_works "$candidate" "$1"; then
      gemini_python="$candidate"
      return 0
    fi
  done
  return 1
}

gemini_bootstrap_python() {
  local system_id system_version
  local privilege=(env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l)
  if [ "$(uname -s)" != Linux ] || [ ! -r /etc/os-release ]; then
    printf 'Для автоматической подготовки нужны Debian 12/13 или Ubuntu 22.04/24.04/26.04. На этой системе установите Python 3.10+ вручную.\n' >&2
    return 2
  fi
  system_id=$(sed -n 's/^ID=//p' /etc/os-release | tr -d '\042')
  system_version=$(sed -n 's/^VERSION_ID=//p' /etc/os-release | tr -d '\042')
  case "$system_id:$system_version" in
    debian:12|debian:13|ubuntu:22.04|ubuntu:24.04|ubuntu:26.04) ;;
    *) printf 'Автоподготовка поддерживает Debian 12/13 и Ubuntu 22.04/24.04/26.04.\n' >&2; return 2 ;;
  esac
  if [ "$(id -u)" != 0 ]; then
    if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
      privilege=(sudo -n env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l)
    else
      printf 'Для установки зависимостей запустите --auto от root или настройте доступный sudo.\n' >&2
      return 2
    fi
  fi
  printf 'Устанавливаю Python и базовые зависимости...\n' >&2
  "${privilege[@]}" apt-get -o DPkg::Lock::Timeout=120 update >&2 || return 2
  "${privilege[@]}" apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends curl python3 python3-venv ca-certificates >&2 || return 2
}

if [ "$gemini_auto" = 1 ] && [ "$gemini_help" = 0 ]; then
  for argument in "$@"; do
    case "$argument" in
      -6|--ipv6|--both|--install-browser|--model|--model=*|--key-file|--key-file=*)
        printf 'Параметр %s несовместим с --auto.\n' "$argument" >&2
        exit 64
        ;;
    esac
  done
  if ! gemini_find_python 10 || ! command -v curl >/dev/null 2>&1; then
    gemini_bootstrap_python
    if ! gemini_find_python 10; then
      printf 'Не найден Python 3.10+. Задайте GEMINI_BROWSER_PYTHON с путём к подходящему Python.\n' >&2
      exit 2
    fi
  fi
elif [ "$gemini_browser" = 1 ]; then
  if ! gemini_find_python 8; then
    printf 'Не найден Python. Для установки и проверки одной командой используйте --auto.\n' >&2
    exit 2
  fi
fi
for dependency in curl "$gemini_python"; do
  if ! command -v "$dependency" >/dev/null 2>&1; then
    printf 'Нужен %s. Debian/Ubuntu: apt install curl python3\n' "$dependency" >&2
    exit 2
  fi
done

# Keep this exact payload for the transition into the newly installed venv.
gemini_saved_umask=$(umask)
umask 077
gemini_payload_dir=$(mktemp -d "${TMPDIR:-/tmp}/gemini-check.XXXXXXXX")
trap 'rm -rf -- "$gemini_payload_dir"' EXIT
cat > "$gemini_payload_dir/payload.py" <<'PYTHON_GEMINI_CHECK'
import argparse
import concurrent.futures
import datetime
import getpass
import ipaddress
import json
import os
import re
import select
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

VERSION = "2.4"
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


def _browser_failure(error, page):
    """Return controlled codes, never a Playwright call log or page contents."""
    try:
        closed = page.is_closed()
    except Exception:
        closed = False
    names = {cls.__name__ for cls in type(error).__mro__}
    playwright_error = any(cls.__module__.startswith("playwright.") for cls in type(error).__mro__)
    # Raw errors can contain cookies, chat text or redirect queries. Inspect them
    # locally for known failure patterns, but export only the codes below.
    message = str(error).casefold() if playwright_error else ""
    if playwright_error and ("target crashed" in message or "page crashed" in message):
        kind, reason = "page_crashed", "page_crashed"
    elif closed or (playwright_error and any(text in message for text in (
            "target page, context or browser has been closed", "target closed", "page has been closed"))):
        kind, reason = "page_closed", "page_closed"
    else:
        kind = "timeout" if "TimeoutError" in names else "playwright_error" if playwright_error else "unexpected_error"
        if "intercepts pointer events" in message or "another element would receive the click" in message:
            reason = "blocked_by_overlay"
        elif "not editable" in message or "not an <input>" in message or "not an input" in message:
            reason = "element_not_editable"
        elif "detached" in message or "not attached" in message:
            reason = "element_detached"
        elif "execution context was destroyed" in message or "navigation" in message:
            reason = "navigation_interrupted"
        else:
            reason = "action_timeout" if kind == "timeout" else "dom_error" if playwright_error else "unexpected_error"
    name = type(error).__name__
    return {"kind": kind, "type": name if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", name) else "Error",
            "reason": reason}, closed


def test_browser_page(page, timeout=60, nonce=None, interactive=False):
    """Test the already-open Gemini UI. No navigation, cookies or raw RPC are exported."""
    token = nonce or "CHECK_" + uuid.uuid4().hex[:20]
    result = {"status": "unknown", "detail": "", "submitted": False,
              "response_verified": False, "request_seen": False, "nonce": token,
              "generation_http": 0, "stage": "detect_gate"}
    started = time.monotonic()
    deadline = started + max(0.1, timeout)
    state = {"sending": False, "request_seen": False, "http": 0}
    diagnostics = {"editor_found": False, "editor_filled": False, "submit_attempted": False}
    composer, last_gate = None, ("", "")
    try:
        while time.monotonic() < deadline:
            result["stage"] = "detect_gate"
            if page.is_closed():
                raise RuntimeError("closed page")
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
            result["stage"] = "locate_editor"
            composer = _browser_composer(page)
            if composer is not None:
                diagnostics["editor_found"] = True
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

        result["stage"] = "snapshot_answers"
        before = _browser_assistants(page)
        if token in page.content() or any(token in text for text in before.values()):
            result["detail"] = "контрольный маркер уже был на странице до отправки; свежий ответ не подтверждён"
            return result
        prompt = "Reply with exactly %s. Do not add any other text." % token
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
            result["stage"] = "fill_editor"
            composer.fill(prompt, timeout=max(100, int((deadline - time.monotonic()) * 1000)))
            diagnostics["editor_filled"] = True
            result["stage"] = "submit"
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
            # Playwright also waits for overlays/animations to stop intercepting
            # clicks. Use the user's remaining budget, not a separate 5 s cap.
            remaining = max(100, int((deadline - time.monotonic()) * 1000))
            diagnostics["submit_attempted"] = True
            if button is not None:
                button.click(timeout=remaining)
            else:
                composer.press("Enter", timeout=remaining)
            result["submitted"] = True
            result["stage"] = "wait_answer"
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
                                      stage="complete",
                                      detail="после отправки сообщения получен новый ответ модели с контрольным маркером; Gemini WEB работает сейчас")
                        return result
                page.wait_for_timeout(250)
            result.update(status="timeout", detail=("новый ответ модели не получен за отведённое время"
                          if state["request_seen"] else "отправка сообщения на сервер Google не подтверждена; возможен сбой интерфейса"))
            return result
        finally:
            # A click can dispatch POST and still raise afterwards. Preserve the
            # observed request even when no action result or reply was obtained.
            result["request_seen"], result["generation_http"] = state["request_seen"], state["http"]
            if state["request_seen"]:
                result["submitted"] = True
            for event, listener in (("request", sent), ("response", received)):
                try:
                    page.remove_listener(event, listener)
                except Exception:
                    pass
    except Exception as error:
        result["error"], closed = _browser_failure(error, page)
        diagnostics["page_closed"] = closed
        kind = result["error"]["kind"]
        diagnostics["page_crashed"] = kind == "page_crashed"
        if kind == "page_crashed":
            result.update(status="browser_crashed", detail="Chromium сообщил о падении страницы; причина падения не установлена")
        elif kind == "page_closed":
            result.update(status="browser_closed", detail="страница или браузер закрылись до завершения проверки; причина закрытия не установлена")
        elif kind == "timeout":
            result.update(status="timeout", detail="браузерное действие не завершилось за оставшееся время проверки")
        else:
            result.update(status="unknown", detail="ошибка работы с интерфейсом Gemini; этап и код указаны ниже")
        return result
    finally:
        result["diagnostics"] = diagnostics
        result["elapsed_ms"] = max(0, int((time.monotonic() - started) * 1000))


class IPv4BrowserProxy:
    """Ephemeral loopback CONNECT relay with strictly IPv4 remote sockets."""
    HEADER_LIMIT = 65536
    HEADER_TIMEOUT = 8
    CONNECT_TIMEOUT = 10
    IDLE_TIMEOUT = 60

    def __init__(self):
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._active = set()
        self._connections = []
        self._seen = set()
        self._failures = 0
        self._thread = None
        self._slots = threading.BoundedSemaphore(64)
        owner = self

        class Server(socketserver.ThreadingTCPServer):
            address_family = socket.AF_INET
            allow_reuse_address = True
            daemon_threads = True
            block_on_close = False
            request_queue_size = 64

            def handle_error(self, request, client_address):
                # Do not log request headers, payloads or raw exception details.
                owner._failed()

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                if not owner._slots.acquire(False):
                    owner._reply(self.request, 503, "Service Unavailable")
                    owner._failed()
                    return
                try:
                    owner._serve(self.request)
                finally:
                    owner._slots.release()

        self._server = Server(("127.0.0.1", 0), Handler)
        self.server_url = "http://127.0.0.1:%s" % self._server.server_address[1]

    def start(self):
        if self._closed.is_set():
            raise RuntimeError("IPv4 relay is closed")
        if self._thread is None:
            self._thread = threading.Thread(target=self._server.serve_forever,
                                            kwargs={"poll_interval": 0.1}, daemon=True)
            self._thread.start()
        return self

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self._closed.is_set():
            return
        self._closed.set()
        if self._thread is not None:
            self._server.shutdown()
        self._server.server_close()
        with self._lock:
            sockets = list(self._active)
        for connection in sockets:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def snapshot(self):
        with self._lock:
            return {"family": "ipv4", "connections": [dict(x) for x in self._connections],
                    "failures": self._failures}

    def _failed(self):
        with self._lock:
            self._failures += 1

    def _track(self, connection):
        with self._lock:
            if not self._closed.is_set():
                self._active.add(connection)
                return True
        connection.close()
        return False

    def _release(self, connection):
        with self._lock:
            self._active.discard(connection)
        connection.close()

    @staticmethod
    def _reply(client, status, reason):
        try:
            client.sendall(("HTTP/1.1 %s %s\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
                            % (status, reason)).encode("ascii"))
        except OSError:
            pass

    def _headers(self, client):
        deadline = time.monotonic() + self.HEADER_TIMEOUT
        data = b""
        while not self._closed.is_set():
            marker = data.find(b"\r\n\r\n")
            if marker >= 0:
                if marker + 4 > self.HEADER_LIMIT:
                    raise OverflowError()
                return data[:marker], data[marker + 4:]
            if len(data) > self.HEADER_LIMIT:
                raise OverflowError()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout()
            client.settimeout(remaining)
            chunk = client.recv(8192)
            if not chunk:
                raise ValueError()
            data += chunk
        raise OSError("closed")

    @staticmethod
    def _authority(headers):
        pieces = headers.split(b"\r\n", 1)[0].decode("ascii").split()
        if len(pieces) != 3 or pieces[2] not in ("HTTP/1.0", "HTTP/1.1"):
            raise ValueError()
        if pieces[0] != "CONNECT":
            return None
        authority = pieces[1]
        if authority.count(":") != 1:
            raise ValueError()
        host, raw_port = authority.rsplit(":", 1)
        if (not host or len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9._-]+", host)
                or not raw_port.isdigit() or len(raw_port) > 5):
            raise ValueError()
        host = host.lower().rstrip(".")
        if not host:
            raise ValueError()
        port = int(raw_port)
        if not 1 <= port <= 65535:
            raise ValueError()
        return host.lower().rstrip("."), port

    def _connect(self, host, port):
        deadline = time.monotonic() + self.CONNECT_TIMEOUT
        addresses = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        for family, kind, protocol, _, address in addresses:
            if family != socket.AF_INET or kind != socket.SOCK_STREAM or protocol not in (0, socket.IPPROTO_TCP):
                continue
            try:
                parsed = ipaddress.ip_address(address[0])
                if parsed.version != 4 or int(address[1]) != port:
                    continue
            except (ValueError, TypeError, IndexError):
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self._closed.is_set():
                break
            remote = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if not self._track(remote):
                break
            try:
                remote.settimeout(remaining)
                remote.connect((str(parsed), port))
                peer = ipaddress.ip_address(remote.getpeername()[0])
                if peer.version != 4:
                    raise OSError("unexpected address family")
                key = (host, str(peer), port)
                with self._lock:
                    if key not in self._seen and len(self._connections) < 128:
                        self._seen.add(key)
                        self._connections.append({"host": host, "ip": str(peer), "port": port})
                return remote
            except OSError:
                self._release(remote)
        raise OSError("no IPv4 connection")

    def _tunnel(self, client, remote, pending):
        client.settimeout(5)
        remote.settimeout(5)
        if pending:
            remote.sendall(pending)
        readers = [client, remote]
        destinations = {client: remote, remote: client}
        last_activity = time.monotonic()
        while readers and not self._closed.is_set():
            remaining = self.IDLE_TIMEOUT - (time.monotonic() - last_activity)
            if remaining <= 0:
                break
            ready, _, _ = select.select(readers, [], [], min(1, remaining))
            for source in ready:
                chunk = source.recv(65536)
                destination = destinations[source]
                if chunk:
                    destination.sendall(chunk)
                    last_activity = time.monotonic()
                else:
                    readers.remove(source)
                    try:
                        destination.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

    def _serve(self, client):
        remote = None
        if not self._track(client):
            return
        try:
            try:
                headers, pending = self._headers(client)
                target = self._authority(headers)
            except OverflowError:
                self._failed()
                self._reply(client, 431, "Request Header Fields Too Large")
                return
            except socket.timeout:
                self._failed()
                self._reply(client, 408, "Request Timeout")
                return
            except (ValueError, UnicodeError):
                self._failed()
                self._reply(client, 400, "Bad Request")
                return
            if target is None:
                self._failed()
                self._reply(client, 405, "Method Not Allowed")
                return
            try:
                remote = self._connect(*target)
            except (OSError, ValueError, TypeError):
                self._failed()
                self._reply(client, 502, "Bad Gateway")
                return
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self._tunnel(client, remote, pending)
        except OSError:
            pass
        finally:
            if remote is not None:
                self._release(remote)
            self._release(client)


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


def _probe_browser_session(proxy="", timeout=60, headed=False, profile=None):
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
                settings["bypass"] = "<-loopback>"
                launch["proxy"] = settings
                launch["args"] = ["--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]
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


def probe_browser(proxy="", timeout=60, headed=False, profile=None, ipv4=None):
    force_ipv4 = not proxy if ipv4 is None else ipv4
    if not force_ipv4:
        result = _probe_browser_session(proxy=proxy, timeout=timeout, headed=headed, profile=profile)
        result["transport"] = {"family": "proxy" if proxy else "auto", "connections": [],
                               "failures": 0, "google_ipv4_confirmed": False}
        return result
    if proxy:
        return {"status": "proxy_unsupported", "detail": "IPv4 назначения внешнего прокси не контролируется; проверьте IPv4 сервера без --proxy",
                "ip": "", "submitted": False, "response_verified": False,
                "transport": {"family": "proxy", "connections": [], "failures": 0,
                              "google_ipv4_confirmed": False}}
    try:
        with IPv4BrowserProxy() as relay:
            result = _probe_browser_session(proxy=relay.server_url, timeout=timeout, headed=headed, profile=profile)
            transport = relay.snapshot()
            google = [c for c in transport["connections"] if c["host"] in
                      ("gemini.google.com", "geminiweb-pa.clients6.google.com")]
            transport["google_ipv4_confirmed"] = bool(google) and all(
                valid_ip(c["ip"]) and ipaddress.ip_address(c["ip"]).version == 4 for c in google)
            result["transport"] = transport
            external = valid_ip(result.get("ip"))
            if external and ipaddress.ip_address(external).version != 4:
                result.update(status="family_mismatch", detail="TCP-соединения ограничены IPv4, но Cloudflare видит IPv6; проверьте трансляцию и маршрут выхода")
            elif result["status"] in ("working", "region_blocked", "access_denied") and not transport["google_ipv4_confirmed"]:
                result.update(status="routing_unconfirmed", detail="не подтверждено IPv4-соединение браузера с Google; вывод о доступности этого IPv4 не сделан")
            return result
    except OSError:
        return {"status": "network_error", "detail": "не удалось создать локальный IPv4-канал; переключения на IPv6 не было",
                "ip": "", "submitted": False, "response_verified": False,
                "transport": {"family": "ipv4", "connections": [], "failures": 1,
                              "google_ipv4_confirmed": False}}


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
                            headed=args.headed, profile=profile, ipv4=not bool(args.proxy))
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
    if args.proxy:
        result["warnings"].append("Семейство выхода выбирает внешний прокси; IPv4 назначения для него не гарантирован")
    elif browser.get("transport", {}).get("google_ipv4_confirmed"):
        result["warnings"].append("Соединение браузера с Google подтверждено через IPv4; IPv6 на сервере не изменялся")
    return result


def browser_home():
    configured = os.environ.get("GEMINI_BROWSER_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path(os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")) / "gemini-check"


def _setup_environment():
    return {k: v for k, v in os.environ.items() if k not in ("GEMINI_KEY", "GEMINI_PROXY")}


def _python_supports(executable, venv_support=False, expected_prefix=None):
    if not executable:
        return False
    code = "import sys;sys.exit(sys.version_info < (3,10))"
    if venv_support:
        code = "import venv,ensurepip;" + code
    arguments = []
    if expected_prefix is not None:
        code = ("import sys;from pathlib import Path;"
                "sys.exit(sys.version_info < (3,10) or sys.prefix == sys.base_prefix "
                "or Path(sys.prefix).resolve() != Path(sys.argv[1]).resolve())")
        arguments = [str(expected_prefix)]
    try:
        return subprocess.run([str(executable), "-c", code] + arguments, env=_setup_environment(),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _find_browser_python(venv_support=False):
    candidates = [os.environ.get("GEMINI_BROWSER_PYTHON"), getattr(sys, "_base_executable", None),
                  sys.executable, "/usr/bin/python3", shutil.which("python3")]
    candidates += [shutil.which("python3.%s" % v) for v in range(14, 9, -1)]
    return next((p for p in candidates if p and _python_supports(p, venv_support)), None)


def _auto_linux_privilege():
    try:
        release = {}
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                release[key] = value.strip().strip("\"'")
        supported = (release.get("ID"), release.get("VERSION_ID")) in (
            ("debian", "12"), ("debian", "13"), ("ubuntu", "22.04"),
            ("ubuntu", "24.04"), ("ubuntu", "26.04"))
        if not supported or not shutil.which("apt-get"):
            raise RuntimeError("Автоподготовка поддерживает Debian 12/13 и Ubuntu 22.04/24.04/26.04.")
        if os.geteuid() == 0:
            return []
        if shutil.which("sudo") and subprocess.run(
                ["sudo", "-n", "true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            return ["sudo", "-n"]
    except OSError:
        pass
    raise RuntimeError("Для установки системных зависимостей запустите --auto от root или с доступным sudo.")


def auto_browser_ready():
    executable = browser_home() / "browser-venv" / "bin" / "python"
    if not executable.is_file():
        return False
    smoke = """import sys
from pathlib import Path
from importlib.metadata import version
if (sys.version_info < (3,10) or sys.prefix == sys.base_prefix
        or Path(sys.prefix).resolve() != Path(sys.argv[1]).resolve()
        or version('playwright') != sys.argv[2]):
    sys.exit(1)
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=['--no-proxy-server'])
    browser.close()
"""
    environment = {k: v for k, v in _setup_environment().items() if k not in PROXY_ENV}
    try:
        return subprocess.run([str(executable), "-c", smoke, str(executable.parent.parent), BROWSER_PLAYWRIGHT_VERSION],
                              env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=35).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def install_browser(with_system_deps=False, output=None):
    output = output if output is not None else sys.stdout
    destination = browser_home() / "browser-venv"
    executable = destination / "bin" / "python"
    setup_env = _setup_environment()
    privilege = []
    print("Подготовка Playwright %s и Chromium в %s" % (BROWSER_PLAYWRIGHT_VERSION, destination), file=output, flush=True)
    try:
        if with_system_deps and sys.platform.startswith("linux"):
            privilege = _auto_linux_privilege()
            setup_env.update(DEBIAN_FRONTEND="noninteractive", NEEDRESTART_MODE="l")
            if not _find_browser_python(venv_support=True) or not Path("/etc/ssl/certs/ca-certificates.crt").is_file():
                package_env = dict(setup_env, DEBIAN_FRONTEND="noninteractive")
                for operation in (["update"], ["install", "-y", "--no-install-recommends",
                                               "curl", "python3", "python3-venv", "ca-certificates"]):
                    subprocess.run(privilege + ["env", "DEBIAN_FRONTEND=noninteractive", "NEEDRESTART_MODE=l", "apt-get",
                                                "-o", "DPkg::Lock::Timeout=120"] + operation,
                                   env=package_env, stdout=output, check=True)
        if not _python_supports(executable, expected_prefix=destination):
            selected = _find_browser_python(venv_support=True)
            if not selected:
                raise RuntimeError("Нужен Python 3.10+ с модулем venv. Debian/Ubuntu: apt install python3-venv.")
            selected = str(Path(selected).resolve())
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                if os.path.commonpath((selected, str(destination.resolve()))) == str(destination.resolve()):
                    raise RuntimeError("Для восстановления окружения нужен Python 3.10+ вне папки browser-venv.")
                backup = destination.with_name("browser-venv.previous-" + uuid.uuid4().hex[:8])
                destination.rename(backup)
                print("Предыдущее несовместимое окружение сохранено: %s" % backup, file=output)
            subprocess.run([selected, "-m", "venv", str(destination)], env=setup_env, stdout=output, check=True)
        if not _python_supports(executable, expected_prefix=destination):
            raise RuntimeError("Не удалось создать изолированное окружение Python. Установка пакетов не выполнялась.")
        subprocess.run([str(executable), "-m", "ensurepip", "--upgrade"], env=setup_env, stdout=output, check=True)
        subprocess.run([str(executable), "-m", "pip", "install", "--disable-pip-version-check",
                        "playwright==" + BROWSER_PLAYWRIGHT_VERSION], env=setup_env, stdout=output, check=True)
        if with_system_deps and sys.platform.startswith("linux"):
            subprocess.run(privilege + ["env", "DEBIAN_FRONTEND=noninteractive", "NEEDRESTART_MODE=l",
                                       str(executable), "-m", "playwright", "install-deps", "chromium"],
                           env=setup_env, stdout=output, check=True)
        subprocess.run([str(executable), "-m", "playwright", "install", "chromium"],
                       env=setup_env, stdout=output, check=True)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, subprocess.CalledProcessError):
        print("Установка не завершена. Проверьте доступ к пакетным репозиториям и PyPI/CDN Playwright.", file=sys.stderr)
        return 2
    print("Браузерное окружение подготовлено.", file=output, flush=True)
    if sys.platform.startswith("linux") and not with_system_deps:
        print("Для автоматической установки системных библиотек и проверки используйте --auto.", file=output)
    return 0


def prepare_auto_browser(args):
    if auto_browser_ready():
        return 0
    if install_browser(with_system_deps=sys.platform.startswith("linux"), output=sys.stderr) != 0:
        return 2
    if not auto_browser_ready():
        print("Chromium после установки не запустился. Проверка Gemini не выполнялась; это не блокировка IP.", file=sys.stderr)
        return 2
    return 0


def reexec_auto_browser():
    destination = browser_home() / "browser-venv"
    if Path(sys.prefix).resolve() == destination.resolve() and sys.prefix != sys.base_prefix:
        return
    if os.environ.get("GEMINI_AUTO_REEXEC") == "1":
        raise OSError("managed Python did not enter its virtual environment")
    executable = destination / "bin" / "python"
    # Keep the requested VPN proxy, but never carry an API key into browser mode.
    environment = {k: v for k, v in os.environ.items() if k != "GEMINI_KEY"}
    environment["GEMINI_AUTO_REEXEC"] = "1"
    os.execve(str(executable), [str(executable), __file__] + sys.argv[1:], environment)


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
                  "access_denied": "ДОСТУП ОТКЛОНЁН", "unknown": "НЕ ОПРЕДЕЛЕНО",
                  "browser_closed": "СТРАНИЦА / БРАУЗЕР ЗАКРЫЛИСЬ",
                  "browser_crashed": "СТРАНИЦА CHROMIUM УПАЛА",
                  "family_mismatch": "СЕМЕЙСТВО ВЫХОДА НЕ ПОДТВЕРЖДЕНО",
                  "routing_unconfirmed": "IPv4-МАРШРУТ НЕ ПОДТВЕРЖДЁН"}

BROWSER_STAGES = {"detect_gate": "проверка входа, региона и CAPTCHA", "locate_editor": "поиск редактора",
                  "snapshot_answers": "проверка предыдущих ответов", "fill_editor": "ввод сообщения",
                  "submit": "отправка сообщения", "wait_answer": "ожидание нового ответа", "complete": "завершено"}
BROWSER_ERRORS = {"blocked_by_overlay": "элемент страницы перекрывает кнопку",
                  "element_not_editable": "редактор недоступен для ввода", "element_detached": "элемент исчез из страницы",
                  "navigation_interrupted": "страница сменилась во время действия", "action_timeout": "истёк лимит действия",
                  "page_closed": "страница или браузер закрылись", "page_crashed": "Chromium сообщил о падении страницы",
                  "dom_error": "ошибка взаимодействия с интерфейсом",
                  "unexpected_error": "непредвиденная ошибка проверки"}


def print_report(report):
    print("gemini-check %s | %s" % (VERSION, report["checked_at"]))
    browser_mode = any(item["id"] == "browser" for item in report["results"])
    print("Проверка обычного веб-чата через Chromium, без API." if browser_mode else "Проверка IP и маршрута. WEB и API оцениваются отдельно.")
    for item in report["results"]:
        if item["id"] == "browser":
            b = item["browser"]
            mode = "IPv4 принудительно" if b.get("transport", {}).get("family") == "ipv4" else "внешний прокси"
            print("\nChromium | %s | внешний IP: %s" % (mode, item["ip"] or "не определён"))
            print("  Cloudflare:   страна %s | узел %s | WARP %s" % (b.get("country") or "?", b.get("colo") or "?", b.get("warp") or "?"))
            print("  Gemini WEB:   %s: %s" % (BROWSER_LABELS.get(b["status"], b["status"]), b["detail"]))
            print("  Сообщение:    %s | новый ответ проверен: %s" % ("отправлено" if b.get("submitted") else "не отправлено", "да" if b.get("response_verified") else "нет"))
            if b.get("stage") and not b.get("response_verified"):
                print("  Этап:         %s | прошло %.1f с" % (BROWSER_STAGES.get(b["stage"], "неизвестен"), b.get("elapsed_ms", 0) / 1000))
                print("  Запрос Google: %s | HTTP %s" % ("замечен" if b.get("request_seen") else "не замечен", b.get("generation_http") or "не получен"))
            if b.get("error"):
                error = b["error"]
                print("  Причина:      %s [%s; %s]" % (BROWSER_ERRORS.get(error["reason"], "ошибка проверки"), error["reason"], error["type"]))
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
    parser.add_argument("--auto", action="store_true", help="самостоятельно установить зависимости и проверить веб-чат без API-ключа")
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
    if args.auto:
        args.browser = True
    if not 5 <= args.timeout <= 60:
        parser.error("--timeout должен быть от 5 до 60")
    if not 15 <= args.browser_timeout <= 300:
        parser.error("--browser-timeout должен быть от 15 до 300")
    if args.install_browser and (args.browser or args.family or args.headed or args.browser_profile or args.json or args.save or args.compare or args.proxy or args.proxy_address):
        parser.error("--install-browser запускается отдельно от проверки")
    if args.browser and args.family not in (None, "ipv4"):
        parser.error("прямой браузерный тест работает через IPv4; --ipv6 и --both доступны только для curl")
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
    if args.auto:
        if prepare_auto_browser(args) != 0:
            return 2
        try:
            reexec_auto_browser()
        except OSError:
            print("Не удалось запустить подготовленное окружение браузера. Проверка Gemini не выполнялась.", file=sys.stderr)
            return 2
    if args.browser:
        if not args.json:
            mode = "через внешний прокси" if args.proxy else "через IPv4"
            print("Проверяю обычный Gemini через Chromium %s. Лимит: %s с.\n" % (mode, args.browser_timeout), flush=True)
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
umask "$gemini_saved_umask"
"$gemini_python" "$gemini_payload_dir/payload.py" "$@"
