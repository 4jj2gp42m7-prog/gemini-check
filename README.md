# gemini-check

Проверяет, подходит ли IP сервера или прокси для Gemini: какую страну для него видит Google (YouTube), открывается ли сайт Gemini и, если дать ключ API, отвечает ли сам Gemini или отказывает по стране.

## Запуск

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/4jj2gp42m7-prog/gemini-check/main/gemini-check.sh)
```

Скрипт спросит:

1. **Что проверять.** Enter — IP этого сервера. Или вставьте прокси: `socks5h://логин:пароль@хост:порт`.
2. **Ключ API Gemini.** Enter — пропустить. Ключ бесплатно выдаёт [AI Studio](https://aistudio.google.com/apikey). Ввод скрыт, ключ уходит только в Google.

## Установить командой `gemini-check`

```bash
curl -fsSL https://raw.githubusercontent.com/4jj2gp42m7-prog/gemini-check/main/gemini-check.sh -o /usr/local/bin/gemini-check && chmod +x /usr/local/bin/gemini-check
```

После этого:

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
