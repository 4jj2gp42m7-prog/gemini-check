#!/usr/bin/env bash
# Проверяет, подходит ли IP сервера или прокси для Gemini.
#
# Интерактивно (спросит прокси и ключ API):
#   bash <(curl -fsSL https://raw.githubusercontent.com/4jj2gp42m7-prog/gemini-check/main/gemini-check.sh)
#
# Без вопросов:
#   gemini-check.sh                                   IP этого сервера
#   gemini-check.sh socks5h://логин:пароль@хост:порт  IP прокси
#   GEMINI_KEY=ключ gemini-check.sh                   плюс проверка через API Gemini
#   GEMINI_KEY=- gemini-check.sh                      без API и без вопроса о ключе

BLOCKED="RU BY CN HK MO IR KP CU SY"

# Вопрос пользователю: читаем с терминала, даже если скрипт пришёл через pipe
ask() {
  local prompt="$1" silent="$2" answer=""
  if [ -r /dev/tty ]; then
    if [ "$silent" = 1 ]; then read -r -s -p "$prompt" answer </dev/tty; echo >/dev/tty
    else read -r -p "$prompt" answer </dev/tty; fi
  fi
  echo "$answer"
}

PROXY="$1"
INTERACTIVE=0
if [ -z "$PROXY" ] && [ -z "$GEMINI_KEY" ] && [ -r /dev/tty ]; then INTERACTIVE=1; fi

if [ "$INTERACTIVE" = 1 ]; then
  echo "Что проверяем?"
  echo "  Enter — IP этого сервера"
  echo "  или вставьте прокси: socks5h://логин:пароль@хост:порт"
  PROXY=$(ask "> ")
  echo
  echo "Ключ API Gemini для самой точной проверки (aistudio.google.com/apikey)."
  echo "  Enter — пропустить. Ввод скрыт."
  GEMINI_KEY=$(ask "> " 1)
  echo
fi
[ "$GEMINI_KEY" = "-" ] && GEMINI_KEY=""

if [ -n "$PROXY" ]; then NET=(-x "$PROXY"); WHAT="прокси ${PROXY##*@}"; else NET=(-4); WHAT="этот сервер"; fi
C() { curl -s --max-time 20 "${NET[@]}" "$@"; }
jfield() { grep -o "\"$1\": *\"[^\"]*\"" | head -1 | cut -d'"' -f4; }

echo "Проверяем: $WHAT"
echo

# 1. IP и провайдер по обычным геобазам
INFO=$(C https://ipinfo.io/json)
IP=$(echo "$INFO" | jfield ip)
ORG=$(echo "$INFO" | jfield org)
if [ -z "$IP" ]; then
  echo "Не удалось выйти в интернет${PROXY:+ через прокси}. Проверьте адрес, логин и пароль."
  exit 1
fi
echo "  IP:                    $IP"
echo "  Страна по геобазам:    $(echo "$INFO" | jfield country), $(echo "$INFO" | jfield city)"
echo "  Провайдер:             ${ORG:-?}"
if echo "$ORG" | grep -qiE 'host|cloud|server|data ?cent|vps|ovh|hetzner|digitalocean|amazon|google|microsoft|oracle|contabo|vultr|linode|akamai|leaseweb|m247|datacamp|aeza|selectel|timeweb|alibaba|tencent'; then
  echo "                         похоже на дата-центр: Google такие IP помечает чаще"
fi

# 2. Страна, которую Google определил для IP (YouTube пишет её в код страницы)
YT=$(C -L -b 'SOCS=CAI' -H 'Accept-Language: en-US,en;q=0.9' https://www.youtube.com/)
GL=$(echo "$YT" | grep -o '"GL":"[A-Z]*"' | head -1 | cut -d'"' -f4)
echo "  YouTube показывает:    ${GL:-не удалось определить}"

# 3. Открывается ли сайт Gemini
GEM_CODE=$(C -o /dev/null -w '%{http_code}' -A 'Mozilla/5.0' https://gemini.google.com/)
echo "  Сайт Gemini:           HTTP ${GEM_CODE:-нет ответа}"

# 4. Ответ самого Gemini через API (если дали ключ)
API=""
if [ -n "$GEMINI_KEY" ]; then
  BASE="https://generativelanguage.googleapis.com/v1beta"
  M=$(C -H "x-goog-api-key: $GEMINI_KEY" "$BASE/models" | grep -o '"models/gemini[^"]*flash[^"]*"' | grep -vE 'tts|image|live|audio|embed' | head -1 | tr -d '"')
  R=$(C -H "x-goog-api-key: $GEMINI_KEY" -H 'Content-Type: application/json' \
        -d '{"contents":[{"parts":[{"text":"Say OK"}]}]}' \
        "$BASE/${M:-models/gemini-flash-latest}:generateContent")
  if echo "$R" | grep -q '"candidates"'; then
    API=ok; echo "  API Gemini:            отвечает (${M#models/})"
  elif echo "$R" | grep -qi 'location is not supported'; then
    API=blocked; echo "  API Gemini:            ОТКАЗ по стране (User location is not supported)"
  else
    echo "  API Gemini:            ошибка: $(echo "$R" | jfield message)"
  fi
fi

# Итог
echo
if [ "$API" = blocked ]; then
  echo "ИТОГ: НЕ ПОДХОДИТ. Gemini отказывает по стране${GL:+, YouTube видит $GL}"
elif [ -z "$GL" ]; then
  echo "ИТОГ: страну определить не удалось. Откройте Gemini в браузере через этот IP"
elif [[ " $BLOCKED " == *" $GL "* ]]; then
  echo "ИТОГ: НЕ ПОДХОДИТ. Google считает этот IP: $GL"
elif [ "$API" = ok ]; then
  echo "ИТОГ: ПОДХОДИТ. YouTube видит $GL, API Gemini отвечает"
else
  echo "ИТОГ: ПОДХОДИТ. YouTube видит $GL"
fi
