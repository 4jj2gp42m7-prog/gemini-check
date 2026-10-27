# gemini-check

Проверяет, подходит ли IP сервера или прокси для Gemini: какую страну для него видит Google (YouTube) и, если дать ключ API, отвечает ли сам Gemini или отказывает по стране. Проверка через API опрашивает три лёгкие модели flash-lite одновременно и занимает не больше 10 секунд.

## Запуск

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/4jj2gp42m7-prog/gemini-check/main/gemini-check.sh)
```

Скрипт сам определит внешний IPv4 сервера и спросит только **ключ API Gemini** (Enter — пропустить). Ключ бесплатно выдаёт [AI Studio](https://aistudio.google.com/apikey). Ввод скрыт, ключ уходит только в Google.

Чтобы проверить прокси, допишите его в конец команды:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/4jj2gp42m7-prog/gemini-check/main/gemini-check.sh) socks5h://логин:пароль@хост:порт
```

## Установить командой `gemini-check`

```bash
curl -fsSL https://raw.githubusercontent.com/4jj2gp42m7-prog/gemini-check/main/gemini-check.sh -o /usr/local/bin/gemini-check && chmod +x /usr/local/bin/gemini-check
```

После этого:

| Команда | Что делает |
|---|---|
| `gemini-check` | проверит IPv4 сервера, спросит только ключ API (Enter — пропустить) |
| `gemini-check socks5h://логин:пароль@хост:порт` | проверит прокси без вопросов |
| `GEMINI_KEY=ключ gemini-check` | проверит IP сервера вместе с API Gemini |
| `GEMINI_KEY=- gemini-check` | проверит IP сервера без API и без вопросов |

## Как читать результат

| Строка | Что значит |
|---|---|
| Страна по геобазам | что пишут обычные геобазы. Для Gemini почти не важно |
| Провайдер | домашний провайдер или дата-центр. Дата-центровые IP Google помечает чаще |
| **YouTube показывает** | страна, которую Google определил для IP. Её же видит Gemini. Главная строка |
| Связь с gemini.google | есть ли сетевой доступ к сайту. **Страну не проверяет:** сайт отвечает и там, где Gemini недоступен |
| API Gemini | `отвечает` — всё хорошо. `ОТКАЗ по стране` — Gemini этот IP не пускает. `ключ и страна приняты, но модели заняты` — Google не отказал, модели просто перегружены |
| **ИТОГ** | `ПОДХОДИТ` или `НЕ ПОДХОДИТ` |

Не подходят IP, которые Google относит к RU, BY, CN, HK, MO, IR, KP, CU, SY.
