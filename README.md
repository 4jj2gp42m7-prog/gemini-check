# gemini-check

Проверяет, подходит ли IP сервера или прокси для Gemini: какую страну для него видит Google (YouTube), открывается ли сайт Gemini и, если дать ключ API, отвечает ли сам Gemini или отказывает по стране.

Репозиторий приватный, поэтому для скачивания нужен токен GitHub.

## Токен GitHub (один раз)

1. GitHub → **Settings** → **Developer settings** → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**.
2. **Repository access:** Only select repositories → `gemini-check`.
3. **Permissions → Repository permissions → Contents:** Read-only.
4. **Generate token** и сохраните его (начинается с `github_pat_`).

Токен даёт только чтение этого репозитория.

## Запуск

```bash
read -rsp "GitHub token: " T; echo; bash <(curl -fsSL -H "Authorization: Bearer $T" -H "Accept: application/vnd.github.raw" https://api.github.com/repos/4jj2gp42m7-prog/gemini-check/contents/gemini-check.sh); unset T
```

Команда спросит токен GitHub (ввод скрыт), скачает скрипт и запустит его. Скрипт спросит:

1. **Что проверять.** Enter — IP этого сервера. Или вставьте прокси: `socks5h://логин:пароль@хост:порт`.
2. **Ключ API Gemini.** Enter — пропустить. Ключ бесплатно выдаёт [AI Studio](https://aistudio.google.com/apikey). Ввод скрыт, ключ уходит только в Google.

## Установить командой `gemini-check`

```bash
read -rsp "GitHub token: " T; echo; curl -fsSL -H "Authorization: Bearer $T" -H "Accept: application/vnd.github.raw" https://api.github.com/repos/4jj2gp42m7-prog/gemini-check/contents/gemini-check.sh -o /usr/local/bin/gemini-check && chmod +x /usr/local/bin/gemini-check; unset T
```

После этого токен больше не нужен:

| Команда | Что делает |
|---|---|
| `gemini-check` | спросит, что проверять, и ключ |
| `gemini-check socks5h://логин:пароль@хост:порт` | проверит прокси без вопросов |
| `GEMINI_KEY=ключ gemini-check` | проверит IP сервера вместе с API Gemini |
| `GEMINI_KEY=- gemini-check` | проверит IP сервера без API и без вопросов |

## Как читать результат

| Строка | Что значит |
|---|---|
| Страна по геобазам | что пишут обычные геобазы. Для Gemini почти не важно |
| Провайдер | домашний провайдер или дата-центр. Дата-центровые IP Google помечает чаще |
| **YouTube показывает** | страна, которую Google определил для IP. Её же видит Gemini. Главная строка |
| Сайт Gemini | отвечает ли gemini.google.com |
| API Gemini | `отвечает` — всё хорошо, `ОТКАЗ по стране` — Gemini этот IP не пускает |
| **ИТОГ** | `ПОДХОДИТ` или `НЕ ПОДХОДИТ` |

Не подходят IP, которые Google относит к RU, BY, CN, HK, MO, IR, KP, CU, SY.
