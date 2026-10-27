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

VERSION="1.7"
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

# 3. Есть ли сетевой доступ к gemini.google.com. Страну этот пункт не проверяет:
#    сайт отвечает 200 и там, где Gemini недоступен
GEM_CODE=$(C -o /dev/null -w '%{http_code}' -A 'Mozilla/5.0' https://gemini.google.com/)
if [ "$GEM_CODE" = 200 ]; then GEM_NOTE="есть (страну не проверяет)"; else GEM_NOTE="нет (HTTP ${GEM_CODE:-000})"; fi
echo "  Связь с gemini.google: $GEM_NOTE"

# 4. Ответ самого Gemini через API (если дали ключ): лёгкие модели параллельно, не дольше 10 секунд
API=""
if [ -n "$GEMINI_KEY" ]; then
  BASE="https://generativelanguage.googleapis.com/v1beta"
  OUT=$(curl -s --max-time 15 "${NET[@]}" -w '\n%{http_code}' -H "x-goog-api-key: $GEMINI_KEY" "$BASE/models?pageSize=1000")
  LCODE=${OUT##*$'\n'}; LIST=${OUT%$'\n'*}
  if echo "$LIST" | grep -qi 'location is not supported'; then
    API=blocked; echo "  API Gemini:            ОТКАЗ по стране (User location is not supported)"
  elif [ "$LCODE" != 200 ]; then
    echo "  API Gemini:            ошибка списка моделей (HTTP $LCODE): $(echo "$LIST" | jfield message)"
  else
    # Только лёгкие модели, они меньше всего нагружены: «последняя» flash-lite и две самые новые flash-lite
    NAMES=$(echo "$LIST" | grep -o '"models/gemini-[^"]*"' | tr -d '"' | grep -vE 'tts|image|live|audio|embed|customtools')
    MODELS=$( { echo "$NAMES" | grep -x 'models/gemini-flash-lite-latest'
                echo "$NAMES" | grep -E 'gemini-[0-9][0-9.]*-flash-lite(-preview)?$' | sort -V -r | head -2
              } | awk 'NF && !seen[$0]++' | head -3 )
    TMP=$(mktemp -d)
    i=0
    for m in $MODELS; do
      i=$((i + 1))
      curl -s --max-time 10 "${NET[@]}" -w '\n%{http_code}' -o "$TMP/$i.body" \
        -H "x-goog-api-key: $GEMINI_KEY" -H 'Content-Type: application/json' \
        -d '{"contents":[{"parts":[{"text":"Hi"}]}],"generationConfig":{"maxOutputTokens":8}}' \
        "$BASE/$m:generateContent" > "$TMP/$i.code" 2>/dev/null &
    done
    wait
    echo "  API Gemini, модели:"
    i=0; OKM=""; BUSY=""
    for m in $MODELS; do
      i=$((i + 1))
      CODE=$(tr -d '\n' < "$TMP/$i.code"); BODY=$(cat "$TMP/$i.body" 2>/dev/null)
      case "$CODE" in
        200) note="отвечает"; OKM=${OKM:-$m} ;;
        503) note="перегружена"; BUSY=1 ;;
        429) note="лимит запросов"; BUSY=1 ;;
        404) note="недоступна для этого ключа" ;;
        000|"") note="не ответила за 10 с" ;;
        *)   note="HTTP $CODE: $(echo "$BODY" | jfield message | cut -c1-80)" ;;
      esac
      echo "$BODY" | grep -qi 'location is not supported' && { note="отказ по стране"; API=blocked; }
      printf '    · %-34s %s\n' "${m#models/}" "$note"
    done
    rm -rf "$TMP"
    if [ "$API" = blocked ]; then
      echo "  API Gemini:            ОТКАЗ по стране (User location is not supported)"
    elif [ -n "$OKM" ]; then
      API=ok; echo "  API Gemini:            отвечает (${OKM#models/})"
    elif [ -n "$BUSY" ]; then
      API=accepted; echo "  API Gemini:            ключ и страна приняты, но модели сейчас заняты"
    else
      echo "  API Gemini:            модели не ответили, по API не проверено"
    fi
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
