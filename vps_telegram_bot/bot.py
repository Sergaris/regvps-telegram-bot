"""Регистрация команд Telegram и проверка allowlist."""

import logging
from collections.abc import Awaitable, Callable

from telegram import Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, Defaults

from vps_telegram_bot.config import AppSettings
from vps_telegram_bot.minecraft_handlers import register_minecraft_handlers
from vps_telegram_bot.reglet_brief import format_reglet_telegram
from vps_telegram_bot.regru_client import (
    RegletAction,
    RegRuClient,
    RegRuClientError,
    format_balance_telegram,
)

log = logging.getLogger(__name__)

# Сокращение для сигнатур обработчиков (ruff E501)
Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]

_ACCESS_DENIED_RU = "Нет доступа. Этот бот только для списка доверенных."
_TG_NET_FAIL_RU = (
    "Не удалось связаться с Telegram (таймаут/сеть). "
    "Проверьте интернет и прокси (HTTP(S)_PROXY, NO_PROXY) и повторите команду."
)


def _is_allowed(effective_user_id: int, settings: AppSettings) -> bool:
    return effective_user_id in settings.allowed_telegram_user_ids


def _reg_client(context: ContextTypes.DEFAULT_TYPE) -> RegRuClient:
    client = context.application.bot_data.get("regru")
    if not isinstance(client, RegRuClient):
        msg = "bot_data['regru'] is missing or not RegRuClient (internal error)"
        raise RuntimeError(msg)
    return client


async def _telegram_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Логировать сбои хендлеров; сетевые ошибки Telegram не роняют процесс.

    Args:
        update: Объект `Update` или ``None`` (например, при ошибке в job queue).
        context: Контекст PTB; `context.error` — исключение из хендлера.
    """
    err = context.error
    if isinstance(err, (TimedOut, NetworkError)):
        log.warning("Telegram request failed: %s", err, exc_info=err)
        u = update if isinstance(update, Update) else None
        m = u.effective_message if u is not None else None
        if m is not None:
            try:
                await m.reply_text(_TG_NET_FAIL_RU)
            except (TimedOut, NetworkError):
                log.warning("Could not send Telegram network error hint to chat", exc_info=True)
        return
    log.exception("Unhandled handler error", exc_info=err)


def _wrap(
    action: RegletAction,
) -> Handler:
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        settings: AppSettings = context.application.bot_data["settings"]
        u = update.effective_user
        if u is None or not _is_allowed(u.id, settings):
            m = update.effective_message
            if m is not None:
                await m.reply_text(_ACCESS_DENIED_RU)
            return
        message = update.effective_message
        if message is None:
            return
        if action in {RegletAction.STOP, RegletAction.REBOOT} and context.args != ["confirm"]:
            await message.reply_text(f"Подтвердите действие: /vps_{action.value} confirm")
            return
        regru = _reg_client(context)
        try:
            text = await regru.post_reglet_action(action)
        except RegRuClientError:
            log.exception("reglet action failed: %s", action)
            await message.reply_text("Панель недоступна или отклонила запрос. Повторите позже.")
        else:
            await message.reply_text(text)

    return handler


def _vps_info_handler(settings: AppSettings) -> Handler:
    """Сводка по `GET /reglets` для `REGRU_REGLET_ID`."""

    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        u = update.effective_user
        if u is None or not _is_allowed(u.id, settings):
            m = update.effective_message
            if m is not None:
                await m.reply_text(_ACCESS_DENIED_RU)
            return
        m = update.effective_message
        if m is None:
            return
        regru = _reg_client(context)
        try:
            payload = await regru.fetch_reglets()
            reglet_detail = None
            try:
                one = await regru.fetch_reglet()
                r = one.get("reglet") if isinstance(one, dict) else None
                if isinstance(r, dict):
                    reglet_detail = r
            except RegRuClientError as e:
                log.warning("GET /reglets/{id} (детализация диска) не удался, только список: %s", e)
            text = format_reglet_telegram(
                payload,
                reglet_id=settings.reglet_id,
                reglet_detail=reglet_detail,
            )
        except RegRuClientError:
            log.exception("fetch reglets failed")
            await m.reply_text("Панель недоступна или отклонила запрос. Повторите позже.")
        else:
            await m.reply_text(text)

    return handler


def _vps_balance_handler(settings: AppSettings) -> Handler:
    """`GET /balance_data` — баланс (руб)."""

    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        u = update.effective_user
        if u is None or not _is_allowed(u.id, settings):
            m = update.effective_message
            if m is not None:
                await m.reply_text(_ACCESS_DENIED_RU)
            return
        m = update.effective_message
        if m is None:
            return
        regru = _reg_client(context)
        try:
            root = await regru.fetch_balance_data()
            text = format_balance_telegram(root)
        except RegRuClientError:
            log.exception("fetch balance_data failed")
            await m.reply_text("Панель недоступна или отклонила запрос. Повторите позже.")
        else:
            await m.reply_text(text)

    return handler


def build_application(settings: AppSettings, regru: RegRuClient) -> Application:
    """Собрать `Application` с long polling, командами `/vps_*` и help.

    Args:
        settings: Провалидированные настройки.
        regru: Клиент Reg.ru, будет закрыт в `post_shutdown` при остановке приложения.

    Returns:
        `Application` до `run_polling` / `initialize` / `start` / `stop`.
    """
    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .defaults(Defaults(allow_sending_without_reply=True))
        .connect_timeout(settings.telegram_http_connect_timeout_sec)
        .read_timeout(settings.telegram_http_read_timeout_sec)
        .write_timeout(settings.telegram_http_write_timeout_sec)
        .pool_timeout(settings.telegram_http_pool_timeout_sec)
        .get_updates_connect_timeout(settings.telegram_http_connect_timeout_sec)
        .get_updates_read_timeout(settings.telegram_http_read_timeout_sec)
        .get_updates_write_timeout(settings.telegram_http_write_timeout_sec)
        .get_updates_pool_timeout(settings.telegram_http_pool_timeout_sec)
        .post_shutdown(_post_shutdown_regru(regru))
        .build()
    )
    app.bot_data["settings"] = settings
    app.bot_data["regru"] = regru
    app.add_error_handler(_telegram_error_handler)
    for h in _handler_list(settings):
        app.add_handler(h)
    return app


def _handler_list(
    settings: AppSettings,
) -> list[CommandHandler | CallbackQueryHandler]:
    base: list[CommandHandler | CallbackQueryHandler] = [
        CommandHandler("start", _help_text_handler(settings), block=False),
        CommandHandler("help", _full_help_handler(settings), block=False),
        CommandHandler("vps", _vps_command_handler(settings), block=False),
        CommandHandler("vps_info", _vps_info_handler(settings), block=False),
        CommandHandler("vps_balance", _vps_balance_handler(settings), block=False),
        CommandHandler("vps_start", _wrap(RegletAction.START), block=False),
        CommandHandler("vps_stop", _wrap(RegletAction.STOP), block=False),
        CommandHandler("vps_reboot", _wrap(RegletAction.REBOOT), block=False),
    ]
    base.extend(register_minecraft_handlers(settings))
    return base


def _post_shutdown_regru(regru: RegRuClient) -> Callable[[Application], Awaitable[None]]:
    async def _inner(_application: Application) -> None:
        await regru.aclose()

    return _inner


def _help_text_handler(
    settings: AppSettings,
) -> Handler:
    async def handler(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        m = update.effective_message
        if m is None:
            return
        u = update.effective_user
        if u is not None and not _is_allowed(u.id, settings):
            await m.reply_text(_ACCESS_DENIED_RU)
            return
        await m.reply_text(_long_help_ru())

    return handler


def _full_help_handler(
    settings: AppSettings,
) -> Handler:
    """Полная справка по всем командам (для ``/help``)."""

    async def handler(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        m = update.effective_message
        if m is None:
            return
        u = update.effective_user
        if u is not None and not _is_allowed(u.id, settings):
            await m.reply_text(_ACCESS_DENIED_RU)
            return
        text = _full_help_ru(settings)
        if len(text) <= 4096:
            await m.reply_text(text)
            return
        for chunk in _split_telegram_message_chunks(text, max_len=4000):
            await m.reply_text(chunk)

    return handler


def _split_telegram_message_chunks(text: str, *, max_len: int) -> list[str]:
    """Разбить текст на части не длиннее ``max_len`` (лимит Telegram ~4096).

    Args:
        text: Исходный текст.
        max_len: Максимум символов в одном сообщении.

    Returns:
        Непустые части для последовательных ``reply_text``.
    """

    if not text.strip():
        return []
    if len(text) <= max_len:
        return [text]
    return [text[i : i + max_len] for i in range(0, len(text), max_len)]


def _vps_command_handler(
    settings: AppSettings,
) -> Handler:
    async def handler(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        m = update.effective_message
        if m is None:
            return
        u = update.effective_user
        if u is not None and not _is_allowed(u.id, settings):
            await m.reply_text(_ACCESS_DENIED_RU)
            return
        await m.reply_text(_vps_list_ru())

    return handler


def _long_help_ru() -> str:
    return (
        "Reg.ru CloudVPS: /vps_info, /vps_balance, /vps_start, "
        "/vps_stop confirm, /vps_reboot confirm.\n"
        "Minecraft (через SSH mcops, если заданы MCOPS_SSH_*): "
        "/mc_status, /mc_start, /mc_stop confirm, /mc_restart confirm, /mc_players, /mc_backups, "
        "/mc_backup_manual <manual-1|manual-2|manual-3>.\n"
        "Стек: /stack_status, /stack_start, /stack_stop confirm.\n"
        "Команда /vps — короткий перечень. Полное описание: /help.\n"
        "Не кладите бота на тот же VPS, которым он управляет."
    )


def _full_help_ru(settings: AppSettings) -> str:
    """Текст справки ``/help`` (все команды и назначение)."""

    lines: list[str] = [
        "Справка по командам бота",
        "",
        "Общее",
        "/start — краткое приветствие и перечень команд.",
        "/help — эта справка: все команды и что они делают.",
        "/vps — короткий список команд без пояснений.",
        "",
        "VPS (Reg.ru CloudVPS, API reglet)",
        "/vps_info — статус виртуалки, IP, регион, тариф, диск, образ, последняя операция "
        "(GET /reglets и детали по вашему REGRU_REGLET_ID).",
        "/vps_balance — баланс лицевого счёта в рублях (balance_data).",
        "/vps_start — запуск reglet в панели (POST start).",
        "/vps_stop confirm — остановка reglet; слово confirm обязательно.",
        "/vps_reboot confirm — перезагрузка reglet; confirm обязателен.",
        "Старт/стоп/ребут в панели иногда занимают до минуты.",
        "",
        "Minecraft и стек (удалённо: SSH на хост и python -m mcops.cli в MCOPS_SSH_REMOTE_CWD).",
    ]
    if settings.mcops_remote is None:
        lines.extend(
            [
                "Сейчас MCOPS_SSH_* не настроены: команды /mc_* и /stack_* недоступны.",
                "После настройки SSH (ключ или MCOPS_SSH_PASSWORD) станут активны строки ниже.",
                "",
            ]
        )
    lines.extend(
        [
            "/mc_status — JSON статуса Minecraft (mcops status --json): "
            "systemd, phase, хвост лога.",
            "/mc_start — systemctl start юнита Minecraft на сервере.",
            "/mc_stop confirm — остановка сервиса Minecraft; нужен confirm.",
            "/mc_restart confirm — перезапуск сервиса Minecraft; нужен confirm.",
            "/mc_players — число игроков онлайн (RCON list через mcops).",
            "/mc_backups — список бэкапов (tar и/или мод/SimpleBackups); "
            "кнопки для шага подтверждения отката.",
            "  После /mc_backups: нажмите бэкап → «Да, откатить» запускает "
            "mcops backup restore … --confirm-destructive на хосте.",
            "/mc_backup_manual manual-1|manual-2|manual-3 — ручной tar-слот (mcops backup create).",
            "",
            "/stack_status — сводка VPS (/vps_info) и сразу статус Minecraft по SSH.",
            "/stack_start — запуск VPS в панели, ожидание SSH, затем systemctl start Minecraft.",
            "/stack_stop confirm — остановка Minecraft; при успехе и phase=stopped "
            "— stop VPS в панели; confirm обязателен.",
            "",
            "Безопасность: бот только для user_id из TELEGRAM_ALLOWED_USER_IDS.",
        ]
    )
    return "\n".join(lines)


def _vps_list_ru() -> str:
    return (
        "VPS: /vps_info, /vps_balance, /vps_start, "
        "/vps_stop confirm, /vps_reboot confirm. "
        "Minecraft: /mc_* и стек /stack_* (нужен SSH mcops; stop/restart с confirm). "
        "Операция старт/стоп/ребут в панели иногда длится до минуты."
    )
