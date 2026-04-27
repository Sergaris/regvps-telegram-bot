"""Регистрация команд Telegram и проверка allowlist."""

import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

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
    reglet_panel_action_in_progress_from_list_payload,
)
from vps_telegram_bot.regru_client import (
    RegletAction,
    RegRuClient,
    RegRuClientError,
    format_balance_telegram,
)
from vps_telegram_bot.remote_mcops import run_remote_mcops
from vps_telegram_bot.telegram_inline_kb import pad_message_for_inline_keyboard

log = logging.getLogger(__name__)

# Сокращение для сигнатур обработчиков (ruff E501)
Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]

# После успешного POST start панель иногда ещё не отражает операцию в ``links.actions``:
# держим «запуск в процессе» для UI, пока не истечёт TTL или VPS не станет active.
_REGLET_PENDING_START_KEY = "reglet_pending_start_monotonic"
_REGLET_PENDING_START_TTL_SEC = 900.0

_ACCESS_DENIED_RU = "Нет доступа. Этот бот только для списка доверенных."
_TG_NET_FAIL_RU = (
    "Не удалось связаться с Telegram (таймаут/сеть). "
    "Проверьте интернет и прокси (HTTP(S)_PROXY, NO_PROXY) и повторите команду."
)


def _home_menu_markup() -> InlineKeyboardMarkup:
    """Главное меню бота."""

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("VPS", callback_data="nav:vps"),
                InlineKeyboardButton("Minecraft", callback_data="nav:mc"),
            ],
            [InlineKeyboardButton("Админская чепуха", callback_data="nav:admin")],
        ]
    )


def _vps_menu_markup(*, is_running: bool | None, is_starting: bool = False) -> InlineKeyboardMarkup:
    """Меню действий с Reg.ru VPS: кнопки зависят от статуса (вкл. / выкл. / неизвестно)."""

    back = [InlineKeyboardButton("Назад", callback_data="nav:home")]
    if is_starting:
        return InlineKeyboardMarkup([back])
    if is_running is True:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Перезапуск", callback_data="vps:confirm_reboot"),
                    InlineKeyboardButton("Стоп", callback_data="vps:confirm_stop"),
                ],
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
            [
                InlineKeyboardButton("Перезапуск", callback_data="vps:confirm_reboot"),
                InlineKeyboardButton("Стоп", callback_data="vps:confirm_stop"),
            ],
            back,
        ]
    )


def _vps_tab_title(*, is_running: bool | None, is_starting: bool = False) -> str:
    """Заголовок экрана VPS после проверки статуса."""

    if is_starting:
        return "VPS: операция в панели (in-progress), подождите…"
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
            [
                InlineKeyboardButton("Включить", callback_data="vps:start_from_mc"),
                InlineKeyboardButton("Назад", callback_data="nav:home"),
            ],
        ]
    )


def _minecraft_vps_starting_markup() -> InlineKeyboardMarkup:
    """Экран «VPS запускается» в разделе Minecraft (без повторного «Включить»)."""

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Назад", callback_data="nav:home")],
        ]
    )


async def _open_minecraft_tab(q: CallbackQuery) -> None:
    """Показать меню вкладки Minecraft (после проверки VPS или по «Открыть после запуска»)."""

    mc_mk = minecraft_menu_markup()
    await q.edit_message_text(
        pad_message_for_inline_keyboard("Minecraft", mc_mk),
        reply_markup=mc_mk,
    )


async def _safe_answer_callback(q: CallbackQuery) -> None:
    """Acknowledge a callback without failing the actual operation."""

    try:
        await q.answer()
    except TelegramError:
        log.warning("Could not answer Telegram callback", exc_info=True)


def _is_allowed(effective_user_id: int, settings: AppSettings) -> bool:
    return effective_user_id in settings.allowed_telegram_user_ids


def _reglet_pending_start_deadlines(context: ContextTypes.DEFAULT_TYPE) -> dict[int, float]:
    """Монотонные дедлайны «после POST start ждём active» по user_id (локально процессу)."""

    raw = context.application.bot_data.setdefault(_REGLET_PENDING_START_KEY, {})
    if not isinstance(raw, dict):
        fresh: dict[int, float] = {}
        context.application.bot_data[_REGLET_PENDING_START_KEY] = fresh
        return fresh
    return cast("dict[int, float]", raw)


def _mark_reglet_start_pending_for_user(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Запомнить, что пользователь только что запросил старт VPS (до появления active в API)."""

    until = time.monotonic() + _REGLET_PENDING_START_TTL_SEC
    _reglet_pending_start_deadlines(context)[user_id] = until


def _clear_reglet_start_pending_for_user(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Снять локальный флаг ожидания старта для пользователя."""

    _reglet_pending_start_deadlines(context).pop(user_id, None)


def _reglet_start_pending_for_user(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Есть ли ещё действующий локальный флаг «старт запрошен»."""

    deadlines = _reglet_pending_start_deadlines(context)
    until = deadlines.get(user_id)
    if until is None:
        return False
    if time.monotonic() > until:
        deadlines.pop(user_id, None)
        return False
    return True


def _reglet_ui_starting(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    payload: Mapping[str, Any],
    *,
    reglet_id: int,
) -> bool:
    """Показывать ли блокирующий экран (панель: ``in-progress`` или локальный флаг после POST)."""

    if reglet_panel_action_in_progress_from_list_payload(payload, reglet_id=reglet_id):
        return True
    running = reglet_is_running_from_list_payload(payload, reglet_id=reglet_id)
    if running is True:
        return False
    return _reglet_start_pending_for_user(context, user_id)


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
            home_mk = _home_menu_markup()
            await q.edit_message_text(
                pad_message_for_inline_keyboard("Главное меню", home_mk),
                reply_markup=home_mk,
            )
            return
        if data == "nav:vps":
            regru = _reg_client(context)
            app_settings: AppSettings = context.application.bot_data["settings"]
            uid = u.id
            await _open_vps_tab(q, regru, app_settings, context, uid)
            return
        if data == "nav:admin":
            adm_mk = admin_menu_markup()
            await q.edit_message_text(
                pad_message_for_inline_keyboard("Админская чепуха", adm_mk),
                reply_markup=adm_mk,
            )
            return
        if data == "nav:help":
            help_text = _full_help_ru(settings)
            home_mk = _home_menu_markup()
            if len(help_text) <= 4096:
                await q.edit_message_text(
                    pad_message_for_inline_keyboard(help_text, home_mk),
                    reply_markup=home_mk,
                )
            else:
                short = "Полный список команд не влезает в одно сообщение. Отправьте /help"
                await q.edit_message_text(
                    pad_message_for_inline_keyboard(short, home_mk),
                    reply_markup=home_mk,
                )
            return
        if data == "nav:mc":
            regru = _reg_client(context)
            app_settings: AppSettings = context.application.bot_data["settings"]
            uid = u.id
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
            if running is True:
                _clear_reglet_start_pending_for_user(context, uid)
            if _reglet_ui_starting(context, uid, payload, reglet_id=app_settings.reglet_id):
                start_mk = _minecraft_vps_starting_markup()
                await q.edit_message_text(
                    pad_message_for_inline_keyboard(
                        "В панели Reg.ru для VPS выполняется операция (in-progress). "
                        "Подождите её завершения, затем снова откройте раздел Minecraft.",
                        start_mk,
                    ),
                    reply_markup=start_mk,
                )
                return
            if running is False:
                gate_mk = _minecraft_vps_off_markup()
                await q.edit_message_text(
                    pad_message_for_inline_keyboard(
                        "Чтобы пользоваться разделом Minecraft, сначала включите VPS.",
                        gate_mk,
                    ),
                    reply_markup=gate_mk,
                )
                return
            await _open_minecraft_tab(q)
            return
        if data == "nav:stack":
            stk_mk = _stack_menu_markup()
            await q.edit_message_text(
                pad_message_for_inline_keyboard("Стек: выберите действие", stk_mk),
                reply_markup=stk_mk,
            )
            return

        if data.startswith("adm:"):
            await _handle_admin_button(q, context, settings, data)
            return

        if data.startswith("vps:"):
            await _handle_vps_button(q, context, data)

    return handler


async def _fetch_reglets_payload(regru: RegRuClient) -> Mapping[str, Any] | None:
    """Загрузить JSON списка reglets или ``None`` при ошибке API."""

    try:
        return await regru.fetch_reglets()
    except RegRuClientError:
        log.exception("fetch reglets failed for VPS menu state")
        return None


async def _fetch_reglet_running(regru: RegRuClient, settings: AppSettings) -> bool | None:
    """Узнать по списку reglets, включена ли виртуалка (``active``)."""

    payload = await _fetch_reglets_payload(regru)
    if payload is None:
        return None
    return reglet_is_running_from_list_payload(
        payload,
        reglet_id=settings.reglet_id,
    )


def _vps_tab_state_from_payload(
    payload: Mapping[str, Any] | None,
    settings: AppSettings,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> tuple[bool | None, bool]:
    """Состояние для экрана VPS: ``running`` и флаг «идёт запуск» (панель или локальный TTL)."""

    if payload is None:
        return None, _reglet_start_pending_for_user(context, user_id)
    running = reglet_is_running_from_list_payload(payload, reglet_id=settings.reglet_id)
    if running is True:
        _clear_reglet_start_pending_for_user(context, user_id)
    is_starting = _reglet_ui_starting(context, user_id, payload, reglet_id=settings.reglet_id)
    return running, is_starting


async def _open_vps_tab(
    q: CallbackQuery,
    regru: RegRuClient,
    settings: AppSettings,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> None:
    """Показать экран VPS после запроса статуса к панели (без промежуточного текста)."""

    payload = await _fetch_reglets_payload(regru)
    running, is_starting = _vps_tab_state_from_payload(payload, settings, context, user_id)
    vps_mk = _vps_menu_markup(is_running=running, is_starting=is_starting)
    title = _vps_tab_title(is_running=running, is_starting=is_starting)
    await q.edit_message_text(
        pad_message_for_inline_keyboard(title, vps_mk),
        reply_markup=vps_mk,
    )


def _vps_panel_in_progress_banner(
    payload: Mapping[str, Any] | None,
    settings: AppSettings,
) -> str:
    """Префикс к тексту статуса VPS, если в ``links.actions`` есть ``in-progress`` для reglet."""

    if payload is None:
        return ""
    if not reglet_panel_action_in_progress_from_list_payload(
        payload,
        reglet_id=settings.reglet_id,
    ):
        return ""
    return (
        "В панели Reg.ru выполняется операция над VPS (in-progress). "
        "Поле «Статус» в сводке может ещё не отражать итог — ориентируйтесь на строку "
        "«Панель: … in-progress» ниже.\n\n"
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
        body = format_reglet_telegram(
            payload,
            reglet_id=settings.reglet_id,
            reglet_detail=reglet_detail,
        )
        return _vps_panel_in_progress_banner(payload, settings) + body
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
        await q.edit_message_text(
            pad_message_for_inline_keyboard("Запрашиваю статус VPS...", markup),
            reply_markup=markup,
        )
        text = await _fetch_vps_status_text(regru, settings)
        await q.edit_message_text(
            pad_message_for_inline_keyboard(text, markup),
            reply_markup=markup,
        )
        return
    if data == "adm:vps_balance":
        await q.edit_message_text(
            pad_message_for_inline_keyboard("Запрашиваю баланс VPS...", markup),
            reply_markup=markup,
        )
        try:
            text = format_balance_telegram(await regru.fetch_balance_data())
        except RegRuClientError:
            log.exception("fetch balance_data failed from admin button")
            text = "Панель недоступна или отклонила запрос. Повторите позже."
        await q.edit_message_text(
            pad_message_for_inline_keyboard(text, markup),
            reply_markup=markup,
        )
        return
    if data == "adm:mc_status":
        if remote is None:
            ssh_msg = "SSH к хосту Minecraft не настроен (см. MCOPS_SSH_* в env)."
            await q.edit_message_text(
                pad_message_for_inline_keyboard(ssh_msg, markup),
                reply_markup=markup,
            )
            return
        await q.edit_message_text(
            pad_message_for_inline_keyboard("Запрашиваю статус Minecraft...", markup),
            reply_markup=markup,
        )
        code, out, err = await run_remote_mcops(remote, ["status", "--json"])
        text = (
            out.strip()[:3500]
            if code == 0
            else f"mcops status failed ({code}):\n{err[:1500] or out[:1500]}"
        )
        await q.edit_message_text(
            pad_message_for_inline_keyboard(text, markup),
            reply_markup=markup,
        )
        return
    if data == "adm:mods_plan":
        if remote is None:
            ssh_msg = "SSH к хосту Minecraft не настроен (см. MCOPS_SSH_* в env)."
            await q.edit_message_text(
                pad_message_for_inline_keyboard(ssh_msg, markup),
                reply_markup=markup,
            )
            return
        await admin_panel_run_mods_plan(q, remote)
        return
    if data == "adm:confirm_mods_apply":
        if remote is None:
            ssh_msg = "SSH к хосту Minecraft не настроен (см. MCOPS_SSH_* в env)."
            await q.edit_message_text(
                pad_message_for_inline_keyboard(ssh_msg, markup),
                reply_markup=markup,
            )
            return
        await admin_panel_show_mods_apply_confirm(q)
        return
    if data == "adm:do_mods_apply":
        if remote is None:
            ssh_msg = "SSH к хосту Minecraft не настроен (см. MCOPS_SSH_* в env)."
            await q.edit_message_text(
                pad_message_for_inline_keyboard(ssh_msg, markup),
                reply_markup=markup,
            )
            return
        await admin_panel_run_mods_apply(q, remote)
        return
    if data == "adm:backup_delete_menu":
        if remote is None:
            ssh_msg = "SSH к хосту Minecraft не настроен (см. MCOPS_SSH_* в env)."
            await q.edit_message_text(
                pad_message_for_inline_keyboard(ssh_msg, markup),
                reply_markup=markup,
            )
            return
        await admin_backup_delete_show_catalog(q, context, remote)
        return
    if data == "adm:world_regen_menu":
        if remote is None:
            ssh_msg = "SSH к хосту Minecraft не настроен (см. MCOPS_SSH_* в env)."
            await q.edit_message_text(
                pad_message_for_inline_keyboard(ssh_msg, markup),
                reply_markup=markup,
            )
            return
        await admin_world_regen_show_intro(q)
        return
    if data == "adm:world_regen_confirm":
        if remote is None:
            ssh_msg = "SSH к хосту Minecraft не настроен (см. MCOPS_SSH_* в env)."
            await q.edit_message_text(
                pad_message_for_inline_keyboard(ssh_msg, markup),
                reply_markup=markup,
            )
            return
        await admin_world_regen_show_final_confirm(q)
        return
    if data == "adm:world_regen_do":
        if remote is None:
            ssh_msg = "SSH к хосту Minecraft не настроен (см. MCOPS_SSH_* в env)."
            await q.edit_message_text(
                pad_message_for_inline_keyboard(ssh_msg, markup),
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
    u = q.from_user
    user_id = u.id if u is not None else 0
    if data == "vps:open":
        await _open_vps_tab(q, regru, settings, context, user_id)
        return
    if data == "vps:info":
        payload = await _fetch_reglets_payload(regru)
        running, is_starting = _vps_tab_state_from_payload(payload, settings, context, user_id)
        vps_mk = _vps_menu_markup(is_running=running, is_starting=is_starting)
        await q.edit_message_text(
            pad_message_for_inline_keyboard("VPS: запрашиваю статус...", vps_mk),
            reply_markup=vps_mk,
        )
        text = await _fetch_vps_status_text(regru, settings)
        payload_after = await _fetch_reglets_payload(regru)
        running_after, is_starting_after = _vps_tab_state_from_payload(
            payload_after,
            settings,
            context,
            user_id,
        )
        vps_mk_after = _vps_menu_markup(is_running=running_after, is_starting=is_starting_after)
        await q.edit_message_text(
            pad_message_for_inline_keyboard(text, vps_mk_after),
            reply_markup=vps_mk_after,
        )
        return
    if data == "vps:balance":
        payload = await _fetch_reglets_payload(regru)
        running, is_starting = _vps_tab_state_from_payload(payload, settings, context, user_id)
        vps_mk = _vps_menu_markup(is_running=running, is_starting=is_starting)
        await q.edit_message_text(
            pad_message_for_inline_keyboard("VPS: запрашиваю баланс...", vps_mk),
            reply_markup=vps_mk,
        )
        try:
            text = format_balance_telegram(await regru.fetch_balance_data())
        except RegRuClientError:
            log.exception("fetch balance_data failed from button")
            text = "Панель недоступна или отклонила запрос. Повторите позже."
        payload_after = await _fetch_reglets_payload(regru)
        running_after, is_starting_after = _vps_tab_state_from_payload(
            payload_after,
            settings,
            context,
            user_id,
        )
        vps_mk_after = _vps_menu_markup(is_running=running_after, is_starting=is_starting_after)
        await q.edit_message_text(
            pad_message_for_inline_keyboard(text, vps_mk_after),
            reply_markup=vps_mk_after,
        )
        return
    if data == "vps:start":
        await _post_vps_button_action(q, regru, RegletAction.START, settings, context, user_id)
        return
    if data == "vps:start_from_mc":
        start_wait_mk = _minecraft_vps_starting_markup()
        await q.edit_message_text(
            pad_message_for_inline_keyboard("VPS: отправляю start...", start_wait_mk),
            reply_markup=start_wait_mk,
        )
        try:
            text = await regru.post_reglet_action(RegletAction.START)
        except RegRuClientError:
            log.exception("reglet start from Minecraft gate failed")
            gate_mk_err = _minecraft_vps_off_markup()
            err_msg = "Панель недоступна или отклонила запрос. Повторите позже."
            await q.edit_message_text(
                pad_message_for_inline_keyboard(err_msg, gate_mk_err),
                reply_markup=gate_mk_err,
            )
            return
        _mark_reglet_start_pending_for_user(context, user_id)
        body = (
            f"{text}\n\n"
            "Дождитесь завершения операции в панели (in-progress), "
            "затем снова откройте раздел Minecraft из главного меню."
        )
        await q.edit_message_text(
            pad_message_for_inline_keyboard(body, start_wait_mk),
            reply_markup=start_wait_mk,
        )
        return
    if data == "vps:confirm_stop":
        confirm_mk = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Да, остановить", callback_data="vps:do_stop"),
                    InlineKeyboardButton("Назад", callback_data="vps:open"),
                ],
                [InlineKeyboardButton("Домой", callback_data="nav:home")],
            ]
        )
        await q.edit_message_text(
            pad_message_for_inline_keyboard("Остановить VPS?", confirm_mk),
            reply_markup=confirm_mk,
        )
        return
    if data == "vps:confirm_reboot":
        confirm_mk = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Да, reboot", callback_data="vps:do_reboot"),
                    InlineKeyboardButton("Назад", callback_data="vps:open"),
                ],
                [InlineKeyboardButton("Домой", callback_data="nav:home")],
            ]
        )
        await q.edit_message_text(
            pad_message_for_inline_keyboard("Перезагрузить VPS?", confirm_mk),
            reply_markup=confirm_mk,
        )
        return
    if data == "vps:do_stop":
        await _post_vps_button_action(q, regru, RegletAction.STOP, settings, context, user_id)
        return
    if data == "vps:do_reboot":
        await _post_vps_button_action(q, regru, RegletAction.REBOOT, settings, context, user_id)


async def _post_vps_button_action(
    q: CallbackQuery,
    regru: RegRuClient,
    action: RegletAction,
    settings: AppSettings,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> None:
    """Post a Reg.ru action and keep the VPS menu attached."""

    payload_before = await _fetch_reglets_payload(regru)
    running_before, is_starting_before = _vps_tab_state_from_payload(
        payload_before,
        settings,
        context,
        user_id,
    )
    vps_mk_before = _vps_menu_markup(is_running=running_before, is_starting=is_starting_before)
    await q.edit_message_text(
        pad_message_for_inline_keyboard(f"VPS: отправляю {action.value}...", vps_mk_before),
        reply_markup=vps_mk_before,
    )
    try:
        text = await regru.post_reglet_action(action)
    except RegRuClientError:
        log.exception("reglet action failed from button: %s", action)
        text = "Панель недоступна или отклонила запрос. Повторите позже."
    else:
        if action == RegletAction.START:
            _mark_reglet_start_pending_for_user(context, user_id)
        elif action in {RegletAction.STOP, RegletAction.REBOOT}:
            _clear_reglet_start_pending_for_user(context, user_id)
    payload_after = await _fetch_reglets_payload(regru)
    running_after, is_starting_after = _vps_tab_state_from_payload(
        payload_after,
        settings,
        context,
        user_id,
    )
    vps_mk_after = _vps_menu_markup(is_running=running_after, is_starting=is_starting_after)
    await q.edit_message_text(
        pad_message_for_inline_keyboard(text, vps_mk_after),
        reply_markup=vps_mk_after,
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
        home_mk = _home_menu_markup()
        await m.reply_text(
            pad_message_for_inline_keyboard(_start_brief_ru(), home_mk),
            reply_markup=home_mk,
        )

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
            home_mk = _home_menu_markup()
            await m.reply_text(
                pad_message_for_inline_keyboard(text, home_mk),
                reply_markup=home_mk,
            )
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
        vps_mk = _vps_menu_markup(is_running=None)
        await m.reply_text(
            pad_message_for_inline_keyboard("VPS", vps_mk),
            reply_markup=vps_mk,
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
