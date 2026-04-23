"""Регистрация команд Telegram и проверка allowlist."""

import logging
from collections.abc import Awaitable, Callable

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, Defaults

from vps_telegram_bot.config import AppSettings
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


def _is_allowed(effective_user_id: int, settings: AppSettings) -> bool:
    return effective_user_id in settings.allowed_telegram_user_ids


def _reg_client(context: ContextTypes.DEFAULT_TYPE) -> RegRuClient:
    client = context.application.bot_data.get("regru")
    if not isinstance(client, RegRuClient):
        msg = "bot_data['regru'] is missing or not RegRuClient (internal error)"
        raise RuntimeError(msg)
    return client


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
        .post_shutdown(_post_shutdown_regru(regru))
        .build()
    )
    app.bot_data["settings"] = settings
    app.bot_data["regru"] = regru
    for h in _handler_list(settings):
        app.add_handler(h)
    return app


def _handler_list(
    settings: AppSettings,
) -> list[CommandHandler]:
    return [
        CommandHandler("start", _help_text_handler(settings), block=False),
        CommandHandler("vps", _vps_command_handler(settings), block=False),
        CommandHandler("vps_info", _vps_info_handler(settings), block=False),
        CommandHandler("vps_balance", _vps_balance_handler(settings), block=False),
        CommandHandler("vps_start", _wrap(RegletAction.START), block=False),
        CommandHandler("vps_stop", _wrap(RegletAction.STOP), block=False),
        CommandHandler("vps_reboot", _wrap(RegletAction.REBOOT), block=False),
    ]


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
        "Reg.ru CloudVPS: /vps_info, /vps_balance, /vps_start, /vps_stop, /vps_reboot.\n"
        "Команда /vps — список. Не кладите бота на тот же VPS, которым он управляет."
    )


def _vps_list_ru() -> str:
    return (
        "VPS: /vps_info, /vps_balance, /vps_start, /vps_stop, /vps_reboot. "
        "Операция старт/стоп/ребут в панели иногда длится до минуты."
    )
