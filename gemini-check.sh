#!/usr/bin/env bash
# Проверяет, подходит ли IP сервера или прокси для Gemini.
#
# Проверить IPv4 этого сервера (спросит только ключ API, Enter — пропустить):
#   bash <(curl -fsSL https://raw.githubusercontent.com/4jj2gp42m7-prog/gemini-check/main/gemini-check.sh)
#
# Без вопросов:
#   gemini-check.sh socks5h://логин:пароль@хост:порт  IP прокси
#   GEMINI_KEY=ключ gemini-check.sh                   плюс проверка через API Gemini
#   GEMINI_KEY=- gemini-check.sh                      без API и без вопроса о ключе

VERSION="1.5"
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
if [ -z "$PROXY" ] && [ -z "$GEMINI_KEY" ] && [ -r /dev/tty ]; then
  echo "Ключ API Gemini для самой точной проверки (aistudio.google.com/apikey)."
  echo "  Enter — пропустить. Ввод скрыт."
  GEMINI_KEY=$(ask "> " 1)
  echo
fi
[ "$GEMINI_KEY" = "-" ] && GEMINI_KEY=""

# Без аргумента проверяем внешний IPv4 самого сервера
if [ -n "$PROXY" ]; then NET=(-x "$PROXY"); WHAT="прокси ${PROXY##*@}"; else NET=(-4); WHAT="IPv4 этого сервера"; fi
C() { curl -s --max-time 20 "${NET[@]}" "$@"; }
jfield() { grep -o "\"$1\": *\"[^\"]*\"" | head -1 | cut -d'"' -f4; }

echo "Проверяем: $WHAT   (gemini-check $VERSION)"
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
  # Запрос к модели. Новые модели перед ответом «думают», поэтому ждём до 60 секунд.
  # Ответ кладём в R, HTTP-код в CODE.
  gen() {
    local out
    out=$(curl -s --max-time 60 "${NET[@]}" -w '\n%{http_code}' \
      -H "x-goog-api-key: $GEMINI_KEY" -H 'Content-Type: application/json' \
      -d '{"contents":[{"parts":[{"text":"Say OK"}]}]}' "$BASE/$1:generateContent")
    CODE=${out##*$'\n'}; R=${out%$'\n'*}
  }
  # Сначала псевдоним последней flash-модели. Если его нет или модель сняли,
  # берём ту, что советует сам API, иначе самую новую flash из списка.
  M=models/gemini-flash-latest
  gen "$M"
  for _ in 1 2 3; do
    echo "$R" | grep -qiE '"candidates"|location is not supported' && break
    S=$(echo "$R" | grep -o 'use models/gemini-[^ ,"]*' | head -1 | sed 's/^use //; s/\.$//')
    if [ -z "$S" ]; then
      S=$(C -H "x-goog-api-key: $GEMINI_KEY" "$BASE/models?pageSize=1000" \
        | grep -o '"models/gemini-[0-9][^"]*flash[^"]*"' | tr -d '"' \
        | grep -vE 'tts|image|live|audio|embed|preview|exp|lite' | sort -V -r | head -1)
    fi
    if [ -z "$S" ] || [ "$S" = "$M" ]; then break; fi
    M=$S; gen "$M"
  done
  # Модель перегружена (503) или кончился лимит (429): пробуем другие flash-модели
  if ! echo "$R" | grep -qiE '"candidates"|location is not supported' && { [ "$CODE" = 503 ] || [ "$CODE" = 429 ]; }; then
    for ALT in $(C -H "x-goog-api-key: $GEMINI_KEY" "$BASE/models?pageSize=1000" \
        | grep -o '"models/gemini-[0-9][^"]*flash[^"]*"' | tr -d '"' \
        | grep -vE 'tts|image|live|audio|embed' | sort -V -r | grep -vxF "$M" | head -3); do
      sleep 2
      M=$ALT; gen "$M"
      echo "$R" | grep -qiE '"candidates"|location is not supported' && break
    done
  fi
  if echo "$R" | grep -q '"candidates"'; then
    API=ok; echo "  API Gemini:            отвечает (${M#models/})"
  elif echo "$R" | grep -qi 'location is not supported'; then
    API=blocked; echo "  API Gemini:            ОТКАЗ по стране (User location is not supported)"
  elif [ "$CODE" = 503 ]; then
    API=accepted; echo "  API Gemini:            ключ и страна приняты, но модели сейчас перегружены (HTTP 503)"
  elif [ "$CODE" = 429 ]; then
    API=accepted; echo "  API Gemini:            ключ и страна приняты, но исчерпан лимит запросов (HTTP 429)"
  else
    MSG=$(echo "$R" | jfield message)
    [ -z "$MSG" ] && MSG=$(echo "$R" | tr -s ' \n\t' ' ' | cut -c1-200)
    [ "$CODE" = 000 ] && MSG="нет ответа за 60 секунд"
    echo "  API Gemini:            ошибка (${M#models/}, HTTP ${CODE:-?}): ${MSG:-пустой ответ}"
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
elif [ "$API" = accepted ]; then
  echo "ИТОГ: ПОДХОДИТ. YouTube видит $GL, API Gemini принимает запросы из этой страны"
else
  echo "ИТОГ: ПОДХОДИТ. YouTube видит $GL"
fi
