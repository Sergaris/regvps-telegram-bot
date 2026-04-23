# regvps-telegram-bot

Telegram-бот на Python: запуск, остановка и перезагрузка виртуалки (reglet) через [Reg.ru CloudVPS API](https://api.cloudvps.reg.ru/) — те же `POST` к `/v1/reglets/{id}/actions`, что в вашем `curl` с `{"type":"start"}` / `stop` / `reboot`.

## Важно

- **Не** запускайте процесс бота **на том же VPS**, которым вы управляете. После `stop` бот погаснет вместе с машиной и не сможет снова её включить. Держите бота на другом хосте (дом, второй дешёвый VPS) или на ПК с always-on.
- Секреты (токены) только в **переменных окружения** или в секрет-менеджере; в репозиторий не коммитить.

## Требования

- Python 3.11+

## Установка

```text
cd regvps-telegram-bot
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

## Переменные окружения

| Variable | Description |
|----------|-------------|
| `REGRU_CLOUDVPS_TOKEN` | Bearer-токен API (как в `curl` `-H "Authorization: Bearer ..."`). |
| `REGRU_REGLET_ID` | Числовой id reglet, например `7027955`. |
| `TELEGRAM_BOT_TOKEN` | Токен от [@BotFather](https://t.me/BotFather). |
| `TELEGRAM_ALLOWED_USER_IDS` | Разрешённые id пользователей Telegram, через запятую (только эти увидят команды). |
| `REGRU_CLOUDVPS_API_BASE` | Опционально, по умолчанию `https://api.cloudvps.reg.ru/v1`. |
| `HTTP_REQUEST_TIMEOUT_SEC` | Опционально, таймаут HTTP к панели (сек), по умолчанию `30`. |

Свой `user_id` в Telegram удобно посмотреть, у `@userinfobot` или в логе бота при тесте (сначала allowlist, потом смотрите, кто пишет).

Скопируйте [`.env.example`](.env.example) в `.env` и не коммитьте `.env`.

При запуске `python -m vps_telegram_bot` корневой файл `.env` **подхватывается** (`python-dotenv`, настройка `override=False`): переменные из файла добавляют недостающие ключи, **но не** перезаписывают уже заданные в системе или в оболочке (удобно для prod: секреты в `Environment=`, для локалки — всё в `.env`).

## Команды в чате

- `/start` / `/vps` — краткая справка
- `/vps_info` — кратко: статус, IP, регион, тариф, образ, последняя операция. Запрашиваются `GET /v1/reglets` (список, есть `links.actions`) и **`GET /v1/reglets/{id}`** — по [доке Reg.ru](https://developers.cloudvps.reg.ru/reglets/info.html) поле **`disk_usage` в гигабайтах занятого** (в списке нередко `0.0`); проценты в боте считаются как «занято / размер диска».
- `/vps_balance` — баланс в **рублях** (то же, что `curl` к `https://api.cloudvps.reg.ru/v1/balance_data` и `jq .balance_data.balance`).
- `/vps_start` — `type: start`
- `/vps_stop` — `type: stop`
- `/vps_reboot` — `type: reboot`

## Запуск

```text
# из каталога репо, venv активирован, переменные заданы в окружении
python -m vps_telegram_bot
```

либо (после `pip install -e .`):

```text
vps-telegram
```

## Разработка

```text
ruff check .
ruff format --check .
pytest
```

## Лицензия

Используйте по ситуации; при необходимости добавьте явную лицензию.
