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
| `TELEGRAM_HTTP_CONNECT_TIMEOUT_SEC` | Опционально, connect-timeout HTTPX к `api.telegram.org` (сек), по умолчанию `30`. |
| `TELEGRAM_HTTP_READ_TIMEOUT_SEC` | Опционально, read-timeout для обычных запросов бота (сек), по умолчанию `60`. |
| `TELEGRAM_HTTP_WRITE_TIMEOUT_SEC` | Опционально, write-timeout (сек), по умолчанию `30`. |
| `TELEGRAM_HTTP_POOL_TIMEOUT_SEC` | Опционально, pool-timeout (сек), по умолчанию `20`. |
| `MCOPS_SSH_HOST` | Опционально: хост Minecraft для вызова `mcops` по SSH (`/mc_*`, `/stack_*`). |
| `MCOPS_SSH_USER` | SSH-пользователь на хосте Minecraft (обязателен, если задан `MCOPS_SSH_HOST`). |
| `MCOPS_SSH_IDENTITY_FILE` | Путь к **приватному** ключу SSH (файл должен существовать). |
| `MCOPS_SSH_PASSWORD` | Пароль SSH (в env). Можно задать **вместе с** `MCOPS_SSH_IDENTITY_FILE`: тогда сначала ключ (`ssh -i`), при типичном сбое SSH — пароль через AsyncSSH без проверки `known_hosts` (удобно после смены host key, но хуже против MITM). Только пароль — тоже без проверки host key. |
| `MCOPS_SSH_PORT` | Опционально, по умолчанию `22`. |
| `MCOPS_SSH_REMOTE_CWD` | Опционально, каталог репо ops на сервере, по умолчанию `/opt/minecraft/ops`. |
| `MCOPS_SSH_REMOTE_PYTHON` | Опционально, интерпретатор на сервере, по умолчанию `python3`. |
| `MCOPS_SSH_TIMEOUT_SEC` | Опционально, timeout TCP/SSH-подключения, по умолчанию `60`. |
| `MCOPS_SSH_COMMAND_TIMEOUT_SEC` | Опционально, timeout выполнения удалённой команды, по умолчанию `3600`. |

Свой `user_id` в Telegram удобно посмотреть, у `@userinfobot` или в логе бота при тесте (сначала allowlist, потом смотрите, кто пишет).

Скопируйте [`.env.example`](.env.example) в `.env` и не коммитьте `.env`.

При запуске `python -m vps_telegram_bot` корневой файл `.env` **подхватывается** (`python-dotenv`, настройка `override=False`): переменные из файла добавляют недостающие ключи, **но не** перезаписывают уже заданные в системе или в оболочке (удобно для prod: секреты в `Environment=`, для локалки — всё в `.env`).

Если в логах видно `httpx`/`http_proxy` и таймауты на TLS к Telegram, проверьте корпоративный прокси: либо настройте его до рабочего `api.telegram.org`, либо добавьте хост в `NO_PROXY` / отключите прокси для процесса бота.

## Команды в чате

- `/start` / `/vps` — краткая справка
- `/vps_info` — кратко: статус, IP, регион, тариф, образ, последняя операция. Запрашиваются `GET /v1/reglets` (список, есть `links.actions`) и **`GET /v1/reglets/{id}`** — по [доке Reg.ru](https://developers.cloudvps.reg.ru/reglets/info.html) поле **`disk_usage` в гигабайтах занятого** (в списке нередко `0.0`); проценты в боте считаются как «занято / размер диска».
- `/vps_balance` — баланс в **рублях** (то же, что `curl` к `https://api.cloudvps.reg.ru/v1/balance_data` и `jq .balance_data.balance`).
- `/vps_start` — `type: start`
- `/vps_stop confirm` — `type: stop`
- `/vps_reboot confirm` — `type: reboot`

Если заданы `MCOPS_SSH_*`, бот дополнительно может дергать `mcops` на хосте Minecraft по SSH:

- `/mc_status`, `/mc_start`, `/mc_stop confirm`, `/mc_restart confirm`, `/mc_players`
- `/mc_backups` — список и кнопки для подтверждённого `backup restore`
- `/mc_backup_manual manual-1|manual-2|manual-3` — ручной tar-слот
- `/stack_status`, `/stack_start`, `/stack_stop confirm` — VPS + Minecraft вместе
- Во вкладке **Админская чепуха** (главное меню) кнопки **«Проверить моды»** и **«Обновить моды»** (ряд под «Баланс VPS») вызывают на хосте `mcops mods plan --local` и `mcops mods apply --local` (Modrinth); для apply нужно подтверждение. Те же `MCOPS_SSH_*`, что и для `mc_*`.

Автоматический мониторинг баланса Reg.ru и отсчёт выключения по низкому балансу должны жить **на самой VPS** (`mcops watchdog tick` + systemd timer в `minecraft-server-ops`), а не в боте.

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
