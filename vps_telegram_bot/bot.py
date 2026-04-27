"""Регистрация команд Telegram и проверка allowlist."""

import logging
from collections.abc import Awaitable, Callable

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import NetworkError, TelegramError, TimedOut
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, Defaults

from vps_telegram_bot.config import AppSettings, McopsRemoteSettings
from vps_telegram_bot.minecraft_handlers import (
    admin_backup_delete_show_catalog,
    admin_menu_markup,
    admin_panel_run_mods_apply,
    admin_panel_run_mods_plan,
    admin_panel_show_mods_apply_confirm,
    admin_world_regen_execute,
    admin_world_regen_show_final_confirm,
    admin_world_regen_show_intro,
    minecraft_menu_markup,
    register_minecraft_handlers,
)
from vps_telegram_bot.reglet_brief import (
    format_reglet_telegram,
    reglet_is_running_from_list_payload,
)
from vps_telegram_bot.regru_client import (
    RegletAction,
    RegRuClient,
    RegRuClientError,
    format_balance_telegram,
)
from vps_telegram_bot.remote_mcops import run_remote_mcops
from vps_telegram_bot.telegram_inline_kb import equal_width_inline_row

log = logging.getLogger(__name__)

# Сокращение для сигнатур обработчиков (ruff E501)
Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]

_ACCESS_DENIED_RU = "Нет доступа. Этот бот только для списка доверенных."
_TG_NET_FAIL_RU = (
    "Не удалось связаться с Telegram (таймаут/сеть). "
    "Проверьте интернет и прокси (HTTP(S)_PROXY, NO_PROXY) и повторите команду."
)


def _home_menu_markup() -> InlineKeyboardMarkup:
    """Главное меню бота."""

    return InlineKeyboardMarkup(
        [
            equal_width_inline_row(
                [
                    InlineKeyboardButton("VPS", callback_data="nav:vps"),
                    InlineKeyboardButton("Minecraft", callback_data="nav:mc"),
                ]
            ),
            [InlineKeyboardButton("Админская чепуха", callback_data="nav:admin")],
        ]
    )


def _vps_menu_markup(*, is_running: bool | None) -> InlineKeyboardMarkup:
    """Меню действий с Reg.ru VPS: кнопки зависят от статуса (вкл. / выкл. / неизвестно)."""

    back = [InlineKeyboardButton("Назад", callback_data="nav:home")]
    if is_running is True:
        return InlineKeyboardMarkup(
            [
                equal_width_inline_row(
                    [
                        InlineKeyboardButton("Перезапуск", callback_data="vps:confirm_reboot"),
                        InlineKeyboardButton("Стоп", callback_data="vps:confirm_stop"),
                    ]
                ),
                back,
            ]
        )
    if is_running is False:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Запуск", callback_data="vps:start")],
                back,
            ]
        )
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Запуск", callback_data="vps:start")],
            equal_width_inline_row(
                [
                    InlineKeyboardButton("Перезапуск", callback_data="vps:confirm_reboot"),
                    InlineKeyboardButton("Стоп", callback_data="vps:confirm_stop"),
                ]
            ),
            back,
        ]
    )


def _vps_tab_title(*, is_running: bool | None) -> str:
    """Заголовок экрана VPS после проверки статуса."""

    if is_running is False:
        return "VPS выключен."
    return "VPS"


def _stack_menu_markup() -> InlineKeyboardMarkup:
    """Меню действий со всем стеком VPS + Minecraft."""

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Статус стека", callback_data="stk:status")],
            [InlineKeyboardButton("Запустить стек", callback_data="stk:start")],
            [InlineKeyboardButton("Остановить стек", callback_data="stk:confirm_stop")],
            [InlineKeyboardButton("Домой", callback_data="nav:home")],
        ]
    )


def _minecraft_vps_off_markup() -> InlineKeyboardMarkup:
    """Экран «сначала включите VPS» из раздела Minecraft."""

    return InlineKeyboardMarkup(
        [
            equal_width_inline_row(
                [
                    InlineKeyboardButton("Включить", callback_data="vps:start_from_mc"),
                    InlineKeyboardButton("Назад", callback_data="nav:home"),
                ]
            ),
        ]
    )


async def _open_minecraft_tab(q: CallbackQuery) -> None:
    """Показать меню вкладки Minecraft (после проверки VPS или по «Открыть после запуска»)."""

    await q.edit_message_text("Minecraft", reply_markup=minecraft_menu_markup())


async def _safe_answer_callback(q: CallbackQuery) -> None:
    """Acknowledge a callback without failing the actual operation."""

    try:
        await q.answer()
    except TelegramError:
        log.warning("Could not answer Telegram callback", exc_info=True)


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
        CallbackQueryHandler(
            _menu_callback_router(settings),
            pattern=r"^(nav|vps|adm):[A-Za-z0-9_-]+$",
        ),
    ]
    base.extend(register_minecraft_handlers(settings))
    return base


def _post_shutdown_regru(regru: RegRuClient) -> Callable[[Application], Awaitable[None]]:
    async def _inner(_application: Application) -> None:
        await regru.aclose()

    return _inner


def _menu_callback_router(settings: AppSettings) -> Handler:
    """Route top-level navigation and VPS button actions."""

    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None or q.data is None:
            return
        u = q.from_user
        if u is None or not _is_allowed(u.id, settings):
            await q.answer("Нет доступа.", show_alert=True)
            return

        await _safe_answer_callback(q)
        data = q.data
        if data == "nav:home":
            await q.edit_message_text("Главное меню", reply_markup=_home_menu_markup())
            return
        if data == "nav:vps":
            regru = _reg_client(context)
            app_settings: AppSettings = context.application.bot_data["settings"]
            await _open_vps_tab(q, regru, app_settings)
            return
        if data == "nav:admin":
            await q.edit_message_text("Админская чепуха", reply_markup=admin_menu_markup())
            return
        if data == "nav:help":
            help_text = _full_help_ru(settings)
            if len(help_text) <= 4096:
                await q.edit_message_text(help_text, reply_markup=_home_menu_markup())
            else:
                await q.edit_message_text(
                    "Полный список команд не влезает в одно сообщение. Отправьте /help",
                    reply_markup=_home_menu_markup(),
                )
            return
        if data == "nav:mc":
            regru = _reg_client(context)
            app_settings: AppSettings = context.application.bot_data["settings"]
            try:
                payload = await regru.fetch_reglets()
            except RegRuClientError:
                log.exception("fetch reglets failed before Minecraft tab")
                await _open_minecraft_tab(q)
                return
            running = reglet_is_running_from_list_payload(
                payload,
                reglet_id=app_settings.reglet_id,
            )
            if running is False:
                await q.edit_message_text(
                    "Чтобы пользоваться разделом Minecraft, сначала включите VPS.",
                    reply_markup=_minecraft_vps_off_markup(),
                )
                return
            await _open_minecraft_tab(q)
            return
        if data == "nav:stack":
            await q.edit_message_text("Стек: выберите действие", reply_markup=_stack_menu_markup())
            return

        if data.startswith("adm:"):
            await _handle_admin_button(q, context, settings, data)
            return

        if data.startswith("vps:"):
            await _handle_vps_button(q, context, data)

    return handler


async def _fetch_reglet_running(regru: RegRuClient, settings: AppSettings) -> bool | None:
    """Узнать по списку reglets, включена ли виртуалка (``active``)."""

    try:
        payload = await regru.fetch_reglets()
    except RegRuClientError:
        log.exception("fetch reglets failed for VPS menu state")
        return None
    return reglet_is_running_from_list_payload(
        payload,
        reglet_id=settings.reglet_id,
    )


async def _open_vps_tab(
    q: CallbackQuery,
    regru: RegRuClient,
    settings: AppSettings,
) -> None:
    """Показать экран VPS после запроса статуса к панели."""

    await q.edit_message_text("Проверяю статус VPS...")
    running = await _fetch_reglet_running(regru, settings)
    await q.edit_message_text(
        _vps_tab_title(is_running=running),
        reply_markup=_vps_menu_markup(is_running=running),
    )


async def _fetch_vps_status_text(regru: RegRuClient, settings: AppSettings) -> str:
    """Текст сводки VPS для кнопок и админ-панели."""

    try:
        payload = await regru.fetch_reglets()
        reglet_detail = None
        try:
            one = await regru.fetch_reglet()
            r = one.get("reglet") if isinstance(one, dict) else None
            if isinstance(r, dict):
                reglet_detail = r
        except RegRuClientError:
            pass
        return format_reglet_telegram(
            payload,
            reglet_id=settings.reglet_id,
            reglet_detail=reglet_detail,
        )
    except RegRuClientError:
        log.exception("fetch reglets failed from admin/vps button")
        return "Панель недоступна или отклонила запрос. Повторите позже."


async def _handle_admin_button(
    q: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    settings: AppSettings,
    data: str,
) -> None:
    """Панель «Админская чепуха»: статусы, баланс, Modrinth."""

    regru = _reg_client(context)
    remote: McopsRemoteSettings | None = settings.mcops_remote
    markup = admin_menu_markup()

    if data == "adm:vps_status":
        await q.edit_message_text("Запрашиваю статус VPS...", reply_markup=markup)
        text = await _fetch_vps_status_text(regru, settings)
        await q.edit_message_text(text, reply_markup=markup)
        return
    if data == "adm:vps_balance":
        await q.edit_message_text("Запрашиваю баланс VPS...", reply_markup=markup)
        try:
            text = format_balance_telegram(await regru.fetch_balance_data())
        except RegRuClientError:
            log.exception("fetch balance_data failed from admin button")
            text = "Панель недоступна или отклонила запрос. Повторите позже."
        await q.edit_message_text(text, reply_markup=markup)
        return
    if data == "adm:mc_status":
        if remote is None:
            await q.edit_message_text(
                "SSH к хосту Minecraft не настроен (см. MCOPS_SSH_* в env).",
                reply_markup=markup,
            )
            return
        await q.edit_message_text("Запрашиваю статус Minecraft...", reply_markup=markup)
        code, out, err = await run_remote_mcops(remote, ["status", "--json"])
        text = (
            out.strip()[:3500]
            if code == 0
            else f"mcops status failed ({code}):\n{err[:1500] or out[:1500]}"
        )
        await q.edit_message_text(text, reply_markup=markup)
        return
    if data == "adm:mods_plan":
        if remote is None:
            await q.edit_message_text(
                "SSH к хосту Minecraft не настроен (см. MCOPS_SSH_* в env).",
                reply_markup=markup,
            )
            return
        await admin_panel_run_mods_plan(q, remote)
        return
    if data == "adm:confirm_mods_apply":
        if remote is None:
            await q.edit_message_text(
                "SSH к хосту Minecraft не настроен (см. MCOPS_SSH_* в env).",
                reply_markup=markup,
            )
            return
        await admin_panel_show_mods_apply_confirm(q)
        return
    if data == "adm:do_mods_apply":
        if remote is None:
            await q.edit_message_text(
                "SSH к хосту Minecraft не настроен (см. MCOPS_SSH_* в env).",
                reply_markup=markup,
            )
            return
        await admin_panel_run_mods_apply(q, remote)
        return
    if data == "adm:backup_delete_menu":
        if remote is None:
            await q.edit_message_text(
                "SSH к хосту Minecraft не настроен (см. MCOPS_SSH_* в env).",
                reply_markup=markup,
            )
            return
        await admin_backup_delete_show_catalog(q, context, remote)
        return
    if data == "adm:world_regen_menu":
        if remote is None:
            await q.edit_message_text(
                "SSH к хосту Minecraft не настроен (см. MCOPS_SSH_* в env).",
                reply_markup=markup,
            )
            return
        await admin_world_regen_show_intro(q)
        return
    if data == "adm:world_regen_confirm":
        if remote is None:
            await q.edit_message_text(
                "SSH к хосту Minecraft не настроен (см. MCOPS_SSH_* в env).",
                reply_markup=markup,
            )
            return
        await admin_world_regen_show_final_confirm(q)
        return
    if data == "adm:world_regen_do":
        if remote is None:
            await q.edit_message_text(
                "SSH к хосту Minecraft не настроен (см. MCOPS_SSH_* в env).",
                reply_markup=markup,
            )
            return
        await admin_world_regen_execute(q, remote, seed=None)
        return


async def _handle_vps_button(
    q: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    data: str,
) -> None:
    """Execute a VPS action from an inline button."""

    regru = _reg_client(context)
    settings: AppSettings = context.application.bot_data["settings"]
    if data == "vps:open":
        await _open_vps_tab(q, regru, settings)
        return
    if data == "vps:info":
        running = await _fetch_reglet_running(regru, settings)
        await q.edit_message_text(
            "VPS: запрашиваю статус...",
            reply_markup=_vps_menu_markup(is_running=running),
        )
        text = await _fetch_vps_status_text(regru, settings)
        running_after = await _fetch_reglet_running(regru, settings)
        await q.edit_message_text(
            text,
            reply_markup=_vps_menu_markup(is_running=running_after),
        )
        return
    if data == "vps:balance":
        running = await _fetch_reglet_running(regru, settings)
        await q.edit_message_text(
            "VPS: запрашиваю баланс...",
            reply_markup=_vps_menu_markup(is_running=running),
        )
        try:
            text = format_balance_telegram(await regru.fetch_balance_data())
        except RegRuClientError:
            log.exception("fetch balance_data failed from button")
            text = "Панель недоступна или отклонила запрос. Повторите позже."
        running_after = await _fetch_reglet_running(regru, settings)
        await q.edit_message_text(
            text,
            reply_markup=_vps_menu_markup(is_running=running_after),
        )
        return
    if data == "vps:start":
        await _post_vps_button_action(q, regru, RegletAction.START, settings)
        return
    if data == "vps:start_from_mc":
        await q.edit_message_text(
            "VPS: отправляю start...",
            reply_markup=_minecraft_vps_off_markup(),
        )
        try:
            text = await regru.post_reglet_action(RegletAction.START)
        except RegRuClientError:
            log.exception("reglet start from Minecraft gate failed")
            await q.edit_message_text(
                "Панель недоступна или отклонила запрос. Повторите позже.",
                reply_markup=_minecraft_vps_off_markup(),
            )
            return
        await q.edit_message_text(text, reply_markup=minecraft_menu_markup())
        return
    if data == "vps:confirm_stop":
        await q.edit_message_text(
            "Остановить VPS?",
            reply_markup=InlineKeyboardMarkup(
                [
                    equal_width_inline_row(
                        [
                            InlineKeyboardButton("Да, остановить", callback_data="vps:do_stop"),
                            InlineKeyboardButton("Назад", callback_data="vps:open"),
                        ]
                    ),
                    [InlineKeyboardButton("Домой", callback_data="nav:home")],
                ]
            ),
        )
        return
    if data == "vps:confirm_reboot":
        await q.edit_message_text(
            "Перезагрузить VPS?",
            reply_markup=InlineKeyboardMarkup(
                [
                    equal_width_inline_row(
                        [
                            InlineKeyboardButton("Да, reboot", callback_data="vps:do_reboot"),
                            InlineKeyboardButton("Назад", callback_data="vps:open"),
                        ]
                    ),
                    [InlineKeyboardButton("Домой", callback_data="nav:home")],
                ]
            ),
        )
        return
    if data == "vps:do_stop":
        await _post_vps_button_action(q, regru, RegletAction.STOP, settings)
        return
    if data == "vps:do_reboot":
        await _post_vps_button_action(q, regru, RegletAction.REBOOT, settings)


async def _post_vps_button_action(
    q: CallbackQuery,
    regru: RegRuClient,
    action: RegletAction,
    settings: AppSettings,
) -> None:
    """Post a Reg.ru action and keep the VPS menu attached."""

    running_before = await _fetch_reglet_running(regru, settings)
    await q.edit_message_text(
        f"VPS: отправляю {action.value}...",
        reply_markup=_vps_menu_markup(is_running=running_before),
    )
    try:
        text = await regru.post_reglet_action(action)
    except RegRuClientError:
        log.exception("reglet action failed from button: %s", action)
        text = "Панель недоступна или отклонила запрос. Повторите позже."
    running_after = await _fetch_reglet_running(regru, settings)
    await q.edit_message_text(
        text,
        reply_markup=_vps_menu_markup(is_running=running_after),
    )


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
        await m.reply_text(_start_brief_ru(), reply_markup=_home_menu_markup())

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
            await m.reply_text(text, reply_markup=_home_menu_markup())
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
        await m.reply_text(
            "VPS",
            reply_markup=_vps_menu_markup(is_running=None),
        )

    return handler


def _start_brief_ru() -> str:
    """Короткое приветствие для ``/start``: только смысл кнопок главного меню."""

    return (
        "Главное меню внизу:\n"
        "• VPS — виртуалка в Reg.ru: запуск, стоп, перезапуск.\n"
        "• Minecraft — по SSH: перезапуск сервиса, бэкапы, ручной бэкап.\n"
        "• Админская чепуха — статусы VPS и Minecraft, баланс, "
        "проверка и обновление модов Modrinth, выборочное удаление бэкапов, "
        "перегенерация мира.\n"
        "\n"
        "Полный список команд: /help. Кратко по командам VPS: /vps.\n"
        "Не запускайте бота на той же машине, которой он управляет — иначе после stop "
        "бот тоже отключится."
    )


def _full_help_ru(settings: AppSettings) -> str:
    """Текст справки ``/help`` (все команды и назначение)."""

    lines: list[str] = [
        "Справка по командам бота",
        "",
        "Общее",
        "/start — главное меню: короткое пояснение к кнопкам и ссылка на /help.",
        "/help — эта справка: все команды и что они делают.",
        "/vps — кнопочное меню VPS.",
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
            "/mc_world_regen — как сбросить мир; "
            "/mc_world_regen confirm или confirm <сид> (нужен свежий mcops с --level-seed).",
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
