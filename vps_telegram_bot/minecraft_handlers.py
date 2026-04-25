"""Telegram handlers for remote ``mcops`` and stack control."""

import asyncio
import json
import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from vps_telegram_bot.config import AppSettings, McopsRemoteSettings
from vps_telegram_bot.reglet_brief import format_reglet_telegram
from vps_telegram_bot.regru_client import RegletAction, RegRuClient, RegRuClientError
from vps_telegram_bot.remote_mcops import run_remote_mcops

log = logging.getLogger(__name__)

_BACKUP_CATALOG_KEY = "mcops_backup_catalog"
_ACCESS_DENIED_RU = "Нет доступа. Этот бот только для списка доверенных."
_CALLBACK_PICK = re.compile(r"^mcs:([A-Za-z0-9_-]+):(\d+)$")
_CALLBACK_GO = re.compile(r"^mcy:([A-Za-z0-9_-]+):(\d+)$")
_CALLBACK_NO = re.compile(r"^mcn:([A-Za-z0-9_-]+):(\d+)$")
_CALLBACK_MC = re.compile(r"^mc:([A-Za-z0-9_-]+)$")
_CALLBACK_MANUAL = re.compile(r"^mcm:(manual-[123])$")
_CALLBACK_MANUAL_GO = re.compile(r"^mcmy:(manual-[123])$")
_CALLBACK_STACK = re.compile(r"^stk:([A-Za-z0-9_-]+)$")
_CALLBACK_AB_PICK = re.compile(r"^abp:([A-Za-z0-9_-]+):(\d+)$")
_CALLBACK_AB_YES = re.compile(r"^aby:([A-Za-z0-9_-]+):(\d+)$")
_CALLBACK_AB_NO = re.compile(r"^abx:([A-Za-z0-9_-]+):(\d+)$")
_MANUAL_BACKUP_RE = re.compile(r"^tar:worlds-manual-(manual-[123])-.+\.(?:tar\.gz|tgz)$")


Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def _tail_text(text: str, *, max_len: int) -> str:
    """Return the end of long command output, where failures usually are."""

    clean = text.strip()
    if len(clean) <= max_len:
        return clean
    return "...\n" + clean[-max_len:]


def tail_command_text(text: str, *, max_len: int) -> str:
    """Обрезка длинного вывода команд для сообщений Telegram."""

    return _tail_text(text, max_len=max_len)


def minecraft_menu_markup() -> InlineKeyboardMarkup:
    """Клавиатура вкладки Minecraft (как в макете: перезапуск, бэкапы, ручной бэкап, назад)."""

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Перезапуск", callback_data="mc:confirm_restart")],
            [InlineKeyboardButton("Бэкапы", callback_data="mc:backups")],
            [InlineKeyboardButton("Ручной бэкап", callback_data="mc:manual_menu")],
            [InlineKeyboardButton("Назад", callback_data="nav:home")],
        ]
    )


def admin_menu_markup() -> InlineKeyboardMarkup:
    """Панель «Админская чепуха»: статусы, баланс, моды Modrinth, назад."""

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Статус VPS", callback_data="adm:vps_status"),
                InlineKeyboardButton("Статус Майна", callback_data="adm:mc_status"),
            ],
            [InlineKeyboardButton("Баланс VPS", callback_data="adm:vps_balance")],
            [
                InlineKeyboardButton("Проверить моды", callback_data="adm:mods_plan"),
                InlineKeyboardButton("Обновить моды", callback_data="adm:confirm_mods_apply"),
            ],
            [InlineKeyboardButton("Удалить бэкап", callback_data="adm:backup_delete_menu")],
            [InlineKeyboardButton("Назад", callback_data="nav:home")],
        ]
    )


def admin_mods_apply_confirm_markup() -> InlineKeyboardMarkup:
    """Подтверждение mcops mods apply из панели админа."""

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Да, обновить моды", callback_data="adm:do_mods_apply"),
                InlineKeyboardButton("Назад", callback_data="nav:admin"),
            ],
            [InlineKeyboardButton("Домой", callback_data="nav:home")],
        ]
    )


async def admin_panel_run_mods_plan(
    q: CallbackQuery,
    remote: McopsRemoteSettings,
) -> None:
    """Выполнить ``mods plan --local`` и вернуть клавиатуру админ-панели."""

    await q.edit_message_text("Проверяю обновления модов (mcops mods plan --local)...")
    code, out, err = await run_remote_mcops(remote, ["mods", "plan", "--local"])
    blob = (out + "\n" + err).strip()
    text = (
        _tail_text(blob, max_len=3500)
        if code == 0
        else f"mods plan: код {code}\n{_tail_text(blob, max_len=3200)}"
    )
    await q.edit_message_text(text, reply_markup=admin_menu_markup())


async def admin_panel_show_mods_apply_confirm(q: CallbackQuery) -> None:
    """Экран подтверждения перед ``mods apply --local``."""

    await q.edit_message_text(
        "Применить обновления Modrinth на сервере?\n"
        "Будут скачаны и заменены соответствующие JAR в каталоге mods "
        "(mcops mods apply --local). Перезапуск сервера не выполняется — при необходимости "
        "сделайте это отдельно.",
        reply_markup=admin_mods_apply_confirm_markup(),
    )


async def admin_panel_run_mods_apply(
    q: CallbackQuery,
    remote: McopsRemoteSettings,
) -> None:
    """Выполнить ``mods apply --local`` и вернуть клавиатуру админ-панели."""

    await q.edit_message_text(
        "Применяю обновления модов (mcops mods apply --local).\nЭто может занять несколько минут..."
    )
    code, out, err = await run_remote_mcops(remote, ["mods", "apply", "--local"])
    blob = (out + "\n" + err).strip()
    text = (
        _tail_text(blob, max_len=3500)
        if code == 0
        else f"mods apply: код {code}\n{_tail_text(blob, max_len=3200)}"
    )
    await q.edit_message_text(text, reply_markup=admin_menu_markup())


def _backup_nav_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Назад к Minecraft", callback_data="nav:mc")],
            [InlineKeyboardButton("Домой", callback_data="nav:home")],
        ]
    )


def _admin_backup_delete_nav_markup() -> InlineKeyboardMarkup:
    """Навигация после операций удаления бэкапа из админки."""

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("К админке", callback_data="nav:admin")],
            [InlineKeyboardButton("Домой", callback_data="nav:home")],
        ]
    )


def _manual_backup_markup(
    rows: list[dict[str, Any]] | None = None,
) -> InlineKeyboardMarkup:
    slots = _manual_slot_labels(rows or [])
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(slots["manual-1"], callback_data="mcm:manual-1"),
                InlineKeyboardButton(slots["manual-2"], callback_data="mcm:manual-2"),
            ],
            [InlineKeyboardButton(slots["manual-3"], callback_data="mcm:manual-3")],
            [InlineKeyboardButton("Назад", callback_data="nav:mc")],
            [InlineKeyboardButton("Домой", callback_data="nav:home")],
        ]
    )


def _manual_overwrite_confirm_markup(slot: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Да, перезаписать", callback_data=f"mcmy:{slot}"),
                InlineKeyboardButton("Назад", callback_data="mc:manual_menu"),
            ],
            [InlineKeyboardButton("Домой", callback_data="nav:home")],
        ]
    )


def _stack_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Статус стека", callback_data="stk:status")],
            [InlineKeyboardButton("Запустить стек", callback_data="stk:start")],
            [InlineKeyboardButton("Остановить стек", callback_data="stk:confirm_stop")],
            [InlineKeyboardButton("Домой", callback_data="nav:home")],
        ]
    )


async def _safe_edit_callback_message(
    q: CallbackQuery,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Edit a callback message without losing the underlying long-running operation."""

    try:
        await q.edit_message_text(text, reply_markup=reply_markup)
    except TelegramError:
        log.warning("Could not edit Telegram callback message", exc_info=True)
        return False
    return True


async def _safe_answer_callback(q: CallbackQuery) -> None:
    """Acknowledge a callback without failing the actual operation."""

    try:
        await q.answer()
    except TelegramError:
        log.warning("Could not answer Telegram callback", exc_info=True)


def _manual_slot_labels(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Build button labels showing whether each manual backup slot is occupied."""

    labels = {slot: f"{slot}: пусто" for slot in ("manual-1", "manual-2", "manual-3")}
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        slot_value = str(row.get("slot") or "")
        if slot_value in labels:
            if bool(row.get("occupied")):
                labels[slot_value] = (
                    f"{slot_value}: занят ({_format_backup_mtime(_row_mtime(row))})"
                )
            continue
        entry_id = str(row.get("id") or "")
        match = _MANUAL_BACKUP_RE.match(entry_id)
        if match is None:
            continue
        slot = match.group(1)
        previous = latest.get(slot)
        if previous is None or _row_mtime(row) > _row_mtime(previous):
            latest[slot] = row
    for slot, row in latest.items():
        labels[slot] = f"{slot}: занят ({_format_backup_mtime(_row_mtime(row))})"
    return labels


def _row_mtime(row: dict[str, Any]) -> float:
    value = row.get("mtime")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        return float(str(value or "0"))
    except ValueError:
        return 0.0


def _format_backup_mtime(mtime: float) -> str:
    if mtime <= 0:
        return "дата неизвестна"
    return datetime.fromtimestamp(mtime).strftime("%d.%m %H:%M")


async def _manual_backup_markup_from_remote(remote: McopsRemoteSettings) -> InlineKeyboardMarkup:
    """Fetch current backup catalog and build manual slot buttons."""

    rows = await _manual_slot_rows_from_remote(remote)
    return _manual_backup_markup(rows)


async def _manual_slot_rows_from_remote(remote: McopsRemoteSettings) -> list[dict[str, Any]]:
    """Fetch manual slot status rows from the remote host."""

    code, rows = await _remote_json_lines(remote, ["backup", "manual-slots", "--local", "--json"])
    return rows if code == 0 else []


def _is_manual_slot_occupied(rows: list[dict[str, Any]], slot: str) -> bool:
    for row in rows:
        if str(row.get("slot") or "") == slot:
            return bool(row.get("occupied"))
    return False


def _is_allowed(user_id: int | None, settings: AppSettings) -> bool:
    return user_id is not None and user_id in settings.allowed_telegram_user_ids


def _require_remote(settings: AppSettings) -> McopsRemoteSettings | None:
    return settings.mcops_remote


async def _remote_json_lines(
    remote: McopsRemoteSettings,
    argv: list[str],
) -> tuple[int, list[dict[str, Any]]]:
    code, out, err = await run_remote_mcops(remote, argv)
    if code != 0:
        return code, []
    rows: list[dict[str, Any]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("skip non-json line from mcops: %s", line[:200])
    return code, rows


def _admin_backup_delete_callback_router(settings: AppSettings) -> Handler:
    """Подтверждение и вызов ``mcops backup delete`` из админки."""

    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None or q.data is None:
            return
        u = q.from_user
        if not _is_allowed(u.id if u else None, settings):
            await q.answer("Нет доступа.", show_alert=True)
            return
        await _safe_answer_callback(q)
        remote = _require_remote(settings)
        catalog_map: dict[str, list[tuple[str, str]]] = context.application.bot_data.get(
            _BACKUP_CATALOG_KEY,
            {},
        )
        uid = u.id

        if remote is None:
            await q.edit_message_text(
                "SSH к хосту Minecraft не настроен (см. MCOPS_SSH_* в env).",
                reply_markup=_admin_backup_delete_nav_markup(),
            )
            return

        if (m := _CALLBACK_AB_PICK.match(q.data)) is not None:
            token = m.group(1)
            idx = int(m.group(2))
            catalog = catalog_map.get(f"{uid}:{token}", [])
            if idx < 0 or idx >= len(catalog):
                await q.answer("Список устарел. Откройте «Удалить бэкап» снова.", show_alert=True)
                return
            eid, label = catalog[idx]
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Да, удалить", callback_data=f"aby:{token}:{idx}"),
                        InlineKeyboardButton("Отмена", callback_data=f"abx:{token}:{idx}"),
                    ],
                    [InlineKeyboardButton("К списку", callback_data="adm:backup_delete_menu")],
                    [InlineKeyboardButton("К админке", callback_data="nav:admin")],
                ]
            )
            extra = label.strip() if label.strip() != eid.strip() else ""
            body = (
                "Удалить этот файл бэкапа с диска сервера?\n"
                "Восстановить его уже будет нельзя.\n\n"
                f"{eid}"
                + (f"\n{extra}" if extra else "")
            )
            await q.edit_message_text(text=body, reply_markup=kb)
            return

        if (m := _CALLBACK_AB_NO.match(q.data)) is not None:
            await q.edit_message_text("Удаление отменено.", reply_markup=admin_menu_markup())
            return

        if (m := _CALLBACK_AB_YES.match(q.data)) is not None:
            token = m.group(1)
            idx = int(m.group(2))
            catalog = catalog_map.get(f"{uid}:{token}", [])
            if idx < 0 or idx >= len(catalog):
                await q.answer("Список устарел.", show_alert=True)
                return
            eid, _label = catalog[idx]
            await q.edit_message_text(f"Удаляю бэкап…\n{eid}")
            code, out, err = await run_remote_mcops(
                remote,
                ["backup", "delete", eid, "--local", "--confirm-destructive"],
            )
            tail = tail_command_text(out + "\n" + err, max_len=3200)
            text = (
                f"Удаление завершено. Код {code}\n{tail}"
                if code == 0
                else f"Ошибка удаления. Код {code}\n{tail}"
            )
            await q.edit_message_text(text, reply_markup=_admin_backup_delete_nav_markup())
            return

    return handler


def register_minecraft_handlers(
    settings: AppSettings,
) -> list[CommandHandler | CallbackQueryHandler]:
    """Handlers for Minecraft / stack / backup UX."""

    return [
        CommandHandler("mc_status", _mc_status_handler(settings), block=False),
        CommandHandler("mc_start", _mc_service_handler(settings, "start"), block=False),
        CommandHandler("mc_stop", _mc_service_handler(settings, "stop"), block=False),
        CommandHandler("mc_restart", _mc_service_handler(settings, "restart"), block=False),
        CommandHandler("mc_players", _mc_players_handler(settings), block=False),
        CommandHandler("mc_backups", _mc_backups_handler(settings), block=False),
        CommandHandler(
            "mc_backup_manual",
            _mc_backup_manual_handler(settings),
            block=False,
        ),
        CommandHandler("stack_status", _stack_status_handler(settings), block=False),
        CommandHandler("stack_start", _stack_start_handler(settings), block=False),
        CommandHandler("stack_stop", _stack_stop_handler(settings), block=False),
        CallbackQueryHandler(
            _minecraft_callback_router(settings),
            pattern=r"^mcs:[A-Za-z0-9_-]+:\d+$",
        ),
        CallbackQueryHandler(
            _minecraft_callback_router(settings),
            pattern=r"^mcy:[A-Za-z0-9_-]+:\d+$",
        ),
        CallbackQueryHandler(
            _minecraft_callback_router(settings),
            pattern=r"^mcn:[A-Za-z0-9_-]+:\d+$",
        ),
        CallbackQueryHandler(
            _minecraft_callback_router(settings),
            pattern=r"^mc:[A-Za-z0-9_-]+$",
        ),
        CallbackQueryHandler(
            _minecraft_callback_router(settings),
            pattern=r"^mcm:manual-[123]$",
        ),
        CallbackQueryHandler(
            _minecraft_callback_router(settings),
            pattern=r"^mcmy:manual-[123]$",
        ),
        CallbackQueryHandler(
            _minecraft_callback_router(settings),
            pattern=r"^stk:[A-Za-z0-9_-]+$",
        ),
        CallbackQueryHandler(
            _admin_backup_delete_callback_router(settings),
            pattern=r"^ab[pyx]:[A-Za-z0-9_-]+:\d+$",
        ),
    ]


def _mc_status_handler(settings: AppSettings) -> Handler:
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        u = update.effective_user
        if msg is None or not _is_allowed(u.id if u else None, settings):
            if msg:
                await msg.reply_text(_ACCESS_DENIED_RU)
            return
        remote = _require_remote(settings)
        if remote is None:
            await msg.reply_text("SSH к хосту Minecraft не настроен (см. MCOPS_SSH_* в env).")
            return
        code, out, err = await run_remote_mcops(remote, ["status", "--json"])
        if code != 0:
            await msg.reply_text(f"mcops status failed ({code}):\n{err[:1500] or out[:1500]}")
            return
        await msg.reply_text(out.strip()[:3900], reply_markup=minecraft_menu_markup())

    return handler


def _mc_service_handler(settings: AppSettings, action: str) -> Handler:
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        u = update.effective_user
        if msg is None or not _is_allowed(u.id if u else None, settings):
            if msg:
                await msg.reply_text(_ACCESS_DENIED_RU)
            return
        remote = _require_remote(settings)
        if remote is None:
            await msg.reply_text("SSH к хосту Minecraft не настроен (см. MCOPS_SSH_* в env).")
            return
        if action in {"stop", "restart"} and (context.args or []) != ["confirm"]:
            await msg.reply_text(f"Подтвердите действие: /mc_{action} confirm")
            return
        await msg.reply_text(f"Minecraft: отправляю systemctl {action}…")
        code, out, err = await run_remote_mcops(remote, ["service", action, "--local"])
        tail = _tail_text(out + "\n" + err, max_len=3500)
        await msg.reply_text(f"Код {code}\n{tail}", reply_markup=minecraft_menu_markup())

    return handler


def _mc_players_handler(settings: AppSettings) -> Handler:
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        u = update.effective_user
        if msg is None or not _is_allowed(u.id if u else None, settings):
            if msg:
                await msg.reply_text(_ACCESS_DENIED_RU)
            return
        remote = _require_remote(settings)
        if remote is None:
            await msg.reply_text("SSH к хосту Minecraft не настроен (см. MCOPS_SSH_* в env).")
            return
        code, out, err = await run_remote_mcops(remote, ["players", "count", "--local"])
        if code != 0:
            await msg.reply_text(f"players count failed ({code}):\n{(err or out)[:1500]}")
            return
        await msg.reply_text(
            f"Игроков онлайн (по RCON list): {out.strip()}",
            reply_markup=minecraft_menu_markup(),
        )

    return handler


def _mc_backups_handler(settings: AppSettings) -> Handler:
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        u = update.effective_user
        if msg is None or not _is_allowed(u.id if u else None, settings):
            if msg:
                await msg.reply_text(_ACCESS_DENIED_RU)
            return
        remote = _require_remote(settings)
        if remote is None:
            await msg.reply_text("SSH к хосту Minecraft не настроен (см. MCOPS_SSH_* в env).")
            return
        code, rows = await _remote_json_lines(remote, ["backup", "list", "--local", "--json"])
        if code != 0:
            await msg.reply_text("backup list: ошибка удалённого mcops.")
            return
        if not rows:
            await msg.reply_text("Бэкапы не найдены (пустой каталог или не настроен мод-путь).")
            return
        catalog: list[tuple[str, str]] = []
        buttons: list[list[InlineKeyboardButton]] = []
        token = secrets.token_urlsafe(6)
        for idx, row in enumerate(rows[:20]):
            eid = str(row.get("id") or "")
            label = str(row.get("label") or eid)[:40]
            if not eid:
                continue
            catalog.append((eid, label))
            buttons.append([InlineKeyboardButton(label, callback_data=f"mcs:{token}:{idx}")])
        buttons.append([InlineKeyboardButton("Назад", callback_data="nav:mc")])
        buttons.append([InlineKeyboardButton("Домой", callback_data="nav:home")])
        uid = u.id if u else 0
        context.application.bot_data.setdefault(_BACKUP_CATALOG_KEY, {})[f"{uid}:{token}"] = catalog
        await msg.reply_text(
            "Последние бэкапы. Нажмите для подтверждения отката:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    return handler


def _mc_backup_manual_handler(settings: AppSettings) -> Handler:
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        u = update.effective_user
        if msg is None or not _is_allowed(u.id if u else None, settings):
            if msg:
                await msg.reply_text(_ACCESS_DENIED_RU)
            return
        remote = _require_remote(settings)
        if remote is None:
            await msg.reply_text("SSH к хосту Minecraft не настроен (см. MCOPS_SSH_* в env).")
            return
        args = context.args or []
        if len(args) != 1 or args[0] not in {"manual-1", "manual-2", "manual-3"}:
            await msg.reply_text(
                "Выберите слот ручного бэкапа:",
                reply_markup=await _manual_backup_markup_from_remote(remote),
            )
            return
        slot = args[0]
        rows = await _manual_slot_rows_from_remote(remote)
        if _is_manual_slot_occupied(rows, slot):
            await msg.reply_text(
                f"Слот {slot} уже занят. Перезаписать его новым ручным бэкапом?",
                reply_markup=_manual_overwrite_confirm_markup(slot),
            )
            return
        await msg.reply_text(f"Запускаю ручной бэкап слота {slot}…")
        code, out, err = await run_remote_mcops(
            remote,
            ["backup", "create", "--slot", slot, "--local"],
        )
        tail = _tail_text(out + "\n" + err, max_len=3500)
        await msg.reply_text(
            f"Код {code}\n{tail}",
            reply_markup=await _manual_backup_markup_from_remote(remote),
        )

    return handler


async def _show_backup_catalog(
    q: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    remote: McopsRemoteSettings,
) -> None:
    """Render the backup list as inline buttons."""

    await q.edit_message_text("Загружаю список бэкапов...")
    code, rows = await _remote_json_lines(remote, ["backup", "list", "--local", "--json"])
    if code != 0:
        await q.edit_message_text(
            "backup list: ошибка удалённого mcops.",
            reply_markup=_backup_nav_markup(),
        )
        await _safe_answer_callback(q)
        return
    if not rows:
        await q.edit_message_text(
            "Бэкапы не найдены (пустой каталог или не настроен мод-путь).",
            reply_markup=_backup_nav_markup(),
        )
        await _safe_answer_callback(q)
        return

    catalog: list[tuple[str, str]] = []
    buttons: list[list[InlineKeyboardButton]] = []
    token = secrets.token_urlsafe(6)
    for idx, row in enumerate(rows[:20]):
        eid = str(row.get("id") or "")
        label = str(row.get("label") or eid)[:40]
        if not eid:
            continue
        catalog.append((eid, label))
        buttons.append([InlineKeyboardButton(label, callback_data=f"mcs:{token}:{idx}")])
    buttons.append([InlineKeyboardButton("Назад", callback_data="nav:mc")])
    buttons.append([InlineKeyboardButton("Домой", callback_data="nav:home")])
    uid = q.from_user.id
    context.application.bot_data.setdefault(_BACKUP_CATALOG_KEY, {})[f"{uid}:{token}"] = catalog
    await q.edit_message_text(
        "Последние бэкапы. Нажмите для подтверждения отката:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    await _safe_answer_callback(q)


async def admin_backup_delete_show_catalog(
    q: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    remote: McopsRemoteSettings,
) -> None:
    """Список бэкапов с кнопками выборочного удаления (админка)."""

    await q.edit_message_text("Загружаю список бэкапов для удаления...")
    code, rows = await _remote_json_lines(remote, ["backup", "list", "--local", "--json"])
    if code != 0:
        await q.edit_message_text(
            "backup list: ошибка удалённого mcops.",
            reply_markup=_admin_backup_delete_nav_markup(),
        )
        return
    if not rows:
        await q.edit_message_text(
            "Бэкапы не найдены.",
            reply_markup=_admin_backup_delete_nav_markup(),
        )
        return

    catalog: list[tuple[str, str]] = []
    buttons: list[list[InlineKeyboardButton]] = []
    token = secrets.token_urlsafe(6)
    for idx, row in enumerate(rows[:20]):
        eid = str(row.get("id") or "")
        label = str(row.get("label") or eid)[:40]
        if not eid:
            continue
        catalog.append((eid, label))
        buttons.append([InlineKeyboardButton(label, callback_data=f"abp:{token}:{idx}")])
    buttons.append([InlineKeyboardButton("К админке", callback_data="nav:admin")])
    buttons.append([InlineKeyboardButton("Домой", callback_data="nav:home")])
    uid = q.from_user.id
    context.application.bot_data.setdefault(_BACKUP_CATALOG_KEY, {})[f"{uid}:{token}"] = catalog
    await q.edit_message_text(
        "Выберите бэкап для удаления с диска сервера (безвозвратно):",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _show_manual_backup_slots(q: CallbackQuery, remote: McopsRemoteSettings) -> None:
    """Render manual backup slots with occupied/free state."""

    await q.edit_message_text("Проверяю ручные слоты бэкапов...")
    await q.edit_message_text(
        "Ручные слоты. Нажмите слот, чтобы перезаписать его новым tar-бэкапом:",
        reply_markup=await _manual_backup_markup_from_remote(remote),
    )
    await _safe_answer_callback(q)


async def _run_restore_with_progress(
    q: CallbackQuery,
    remote: McopsRemoteSettings,
    backup_id: str,
) -> None:
    """Run restore and keep Telegram updated while SSH command is running."""

    task = asyncio.create_task(
        run_remote_mcops(
            remote,
            [
                "backup",
                "restore",
                backup_id,
                "--local",
                "--confirm-destructive",
            ],
        )
    )
    elapsed = 0
    while not task.done():
        await asyncio.sleep(10.0)
        elapsed += 10
        await _safe_edit_callback_message(
            q,
            "Restore выполняется.\n"
            f"Бэкап: {backup_id}\n"
            f"Прошло: {elapsed} сек.\n"
            "Minecraft может долго сохраняться перед остановкой.",
        )
    code, out, err = await task
    tail = _tail_text(out + "\n" + err, max_len=3500)
    edited = await _safe_edit_callback_message(
        q,
        f"Restore завершён. Код {code}\n{tail}",
        reply_markup=_backup_nav_markup(),
    )
    if not edited and q.message is not None:
        await q.message.reply_text(
            f"Restore завершён. Код {code}\n{tail}",
            reply_markup=_backup_nav_markup(),
        )


async def _run_manual_backup_with_progress(
    q: CallbackQuery,
    remote: McopsRemoteSettings,
    slot: str,
) -> None:
    """Run manual backup and keep Telegram updated while SSH command is running."""

    task = asyncio.create_task(
        run_remote_mcops(
            remote,
            ["backup", "create", "--slot", slot, "--local"],
        )
    )
    elapsed = 0
    while not task.done():
        await asyncio.sleep(10.0)
        elapsed += 10
        await _safe_edit_callback_message(
            q,
            "Ручной бэкап выполняется.\n"
            f"Слот: {slot}\n"
            f"Прошло: {elapsed} сек.\n"
            "Minecraft останавливается на время архивации мира.",
        )
    code, out, err = await task
    tail = _tail_text(out + "\n" + err, max_len=3500)
    markup = await _manual_backup_markup_from_remote(remote)
    edited = await _safe_edit_callback_message(
        q,
        f"Ручной бэкап завершён. Код {code}\n{tail}",
        reply_markup=markup,
    )
    if not edited and q.message is not None:
        await q.message.reply_text(
            f"Ручной бэкап завершён. Код {code}\n{tail}",
            reply_markup=markup,
        )


async def _handle_minecraft_button(
    q: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    remote: McopsRemoteSettings,
    action: str,
) -> None:
    """Execute Minecraft menu actions."""

    if action == "status":
        await q.edit_message_text("Minecraft: запрашиваю статус...")
        code, out, err = await run_remote_mcops(remote, ["status", "--json"])
        text = (
            out.strip()[:3500]
            if code == 0
            else f"mcops status failed ({code}):\n{err[:1500] or out[:1500]}"
        )
        await q.edit_message_text(text, reply_markup=minecraft_menu_markup())
        await _safe_answer_callback(q)
        return
    if action == "players":
        await q.edit_message_text("Minecraft: считаю игроков...")
        code, out, err = await run_remote_mcops(remote, ["players", "count", "--local"])
        text = (
            f"Игроков онлайн (по RCON list): {out.strip()}"
            if code == 0
            else f"players count failed ({code}):\n{(err or out)[:1500]}"
        )
        await q.edit_message_text(text, reply_markup=minecraft_menu_markup())
        await _safe_answer_callback(q)
        return
    if action == "start":
        await _run_minecraft_service_button(q, remote, "start")
        return
    if action == "confirm_stop":
        await q.edit_message_text(
            "Остановить Minecraft?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Да, остановить", callback_data="mc:do_stop"),
                        InlineKeyboardButton("Назад", callback_data="nav:mc"),
                    ],
                    [InlineKeyboardButton("Домой", callback_data="nav:home")],
                ]
            ),
        )
        await _safe_answer_callback(q)
        return
    if action == "confirm_restart":
        await q.edit_message_text(
            "Перезапустить Minecraft?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Да, restart", callback_data="mc:do_restart"),
                        InlineKeyboardButton("Назад", callback_data="nav:mc"),
                    ],
                    [InlineKeyboardButton("Домой", callback_data="nav:home")],
                ]
            ),
        )
        await _safe_answer_callback(q)
        return
    if action == "do_stop":
        await _run_minecraft_service_button(q, remote, "stop")
        return
    if action == "do_restart":
        await _run_minecraft_service_button(q, remote, "restart")
        return
    if action == "backups":
        await _show_backup_catalog(q, context, remote)
        return
    if action == "manual_menu":
        await _show_manual_backup_slots(q, remote)


async def _run_minecraft_service_button(
    q: CallbackQuery,
    remote: McopsRemoteSettings,
    action: str,
) -> None:
    await q.edit_message_text(f"Minecraft: отправляю systemctl {action}...")
    code, out, err = await run_remote_mcops(remote, ["service", action, "--local"])
    tail = _tail_text(out + "\n" + err, max_len=3000)
    await q.edit_message_text(f"Код {code}\n{tail}", reply_markup=minecraft_menu_markup())
    await _safe_answer_callback(q)


async def _handle_manual_backup_button(
    q: CallbackQuery,
    remote: McopsRemoteSettings,
    slot: str,
) -> None:
    rows = await _manual_slot_rows_from_remote(remote)
    if _is_manual_slot_occupied(rows, slot):
        await q.edit_message_text(
            f"Слот {slot} уже занят. Перезаписать его новым ручным бэкапом?",
            reply_markup=_manual_overwrite_confirm_markup(slot),
        )
        await _safe_answer_callback(q)
        return
    await _start_manual_backup_button(q, remote, slot)


async def _start_manual_backup_button(
    q: CallbackQuery,
    remote: McopsRemoteSettings,
    slot: str,
) -> None:
    await q.edit_message_text(
        f"Запускаю ручной бэкап слота {slot}.\n"
        "Буду обновлять это сообщение, пока команда выполняется."
    )
    await _safe_answer_callback(q)
    await _run_manual_backup_with_progress(q, remote, slot)


async def _handle_stack_button(
    q: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    remote: McopsRemoteSettings | None,
    action: str,
) -> None:
    """Execute stack menu actions."""

    regru = context.application.bot_data.get("regru")
    if not isinstance(regru, RegRuClient):
        await q.edit_message_text(
            "Внутренняя ошибка: нет RegRuClient.",
            reply_markup=_stack_menu_markup(),
        )
        await _safe_answer_callback(q)
        return
    if action == "status":
        await q.edit_message_text("Стек: запрашиваю статус...")
        lines: list[str] = []
        try:
            payload = await regru.fetch_reglets()
            detail = None
            try:
                one = await regru.fetch_reglet()
                r = one.get("reglet") if isinstance(one, dict) else None
                if isinstance(r, dict):
                    detail = r
            except RegRuClientError:
                pass
            lines.append(
                format_reglet_telegram(
                    payload,
                    reglet_id=context.application.bot_data["settings"].reglet_id,
                    reglet_detail=detail,
                )
            )
        except RegRuClientError:
            lines.append("VPS: панель недоступна.")
        if remote is None:
            lines.append("Minecraft: SSH не настроен.")
        else:
            code, out, err = await run_remote_mcops(remote, ["status", "--json"])
            lines.append(f"Minecraft mcops status: код {code}")
            lines.append(_tail_text(out or err, max_len=2200))
            await _append_watchdog_status(lines, remote)
        await q.edit_message_text("\n\n".join(lines)[:3900], reply_markup=_stack_menu_markup())
        await _safe_answer_callback(q)
        return
    if remote is None:
        await q.edit_message_text(
            "SSH к хосту Minecraft не настроен (см. MCOPS_SSH_* в env).",
            reply_markup=_stack_menu_markup(),
        )
        return
    if action == "start":
        await q.edit_message_text("Запускаю VPS, затем дождусь SSH и стартану Minecraft...")
        try:
            await regru.post_reglet_action(RegletAction.START)
        except RegRuClientError:
            await q.edit_message_text(
                "Панель недоступна при запуске VPS.",
                reply_markup=_stack_menu_markup(),
            )
            await _safe_answer_callback(q)
            return
        for attempt in range(60):
            code, _out, _err = await run_remote_mcops(remote, ["status", "--json"])
            if code == 0:
                break
            if attempt % 6 == 0:
                await q.edit_message_text(f"Жду SSH до хоста Minecraft... {attempt * 5} сек")
            await asyncio.sleep(5.0)
        else:
            await q.edit_message_text(
                "SSH так и не ответил. Проверьте сеть/VPS вручную.",
                reply_markup=_stack_menu_markup(),
            )
            await _safe_answer_callback(q)
            return
        code, out, err = await run_remote_mcops(remote, ["service", "start", "--local"])
        tail = _tail_text(out + "\n" + err, max_len=2500)
        await q.edit_message_text(
            f"stack_start: service start код {code}\n{tail}",
            reply_markup=_stack_menu_markup(),
        )
        await _safe_answer_callback(q)
        return
    if action == "confirm_stop":
        await q.edit_message_text(
            "Остановить Minecraft, затем VPS?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Да, остановить стек", callback_data="stk:do_stop"),
                        InlineKeyboardButton("Назад", callback_data="nav:stack"),
                    ],
                    [InlineKeyboardButton("Домой", callback_data="nav:home")],
                ]
            ),
        )
        await _safe_answer_callback(q)
        return
    if action == "do_stop":
        await q.edit_message_text("Останавливаю Minecraft...")
        code, out, err = await run_remote_mcops(remote, ["service", "stop", "--local"])
        if code != 0:
            tail = _tail_text(out + "\n" + err, max_len=2500)
            await q.edit_message_text(
                f"VPS не останавливаю: Minecraft stop завершился с ошибкой.\nКод {code}\n{tail}",
                reply_markup=_stack_menu_markup(),
            )
            await _safe_answer_callback(q)
            return
        status_code, status_out, status_err = await run_remote_mcops(remote, ["status", "--json"])
        if status_code != 0:
            await q.edit_message_text(
                "VPS не останавливаю: не удалось проверить статус Minecraft после stop.\n"
                f"{_tail_text(status_err or status_out, max_len=1500)}",
                reply_markup=_stack_menu_markup(),
            )
            await _safe_answer_callback(q)
            return
        try:
            status_root = json.loads(status_out)
        except json.JSONDecodeError:
            await q.edit_message_text(
                "VPS не останавливаю: mcops status вернул не-JSON.",
                reply_markup=_stack_menu_markup(),
            )
            await _safe_answer_callback(q)
            return
        if status_root.get("phase") != "stopped":
            await q.edit_message_text(
                "VPS не останавливаю: Minecraft не выглядит остановленным "
                f"(phase={status_root.get('phase')}).",
                reply_markup=_stack_menu_markup(),
            )
            await _safe_answer_callback(q)
            return
        await q.edit_message_text("Minecraft остановлен. Останавливаю VPS...")
        try:
            text = await regru.post_reglet_action(RegletAction.STOP)
        except RegRuClientError:
            text = "Панель недоступна при остановке VPS."
        await q.edit_message_text(text, reply_markup=_stack_menu_markup())
        await _safe_answer_callback(q)


def _minecraft_callback_router(settings: AppSettings) -> Handler:
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None or q.data is None:
            return
        u = q.from_user
        if not _is_allowed(u.id if u else None, settings):
            await q.answer("Нет доступа.", show_alert=True)
            return
        await _safe_answer_callback(q)
        remote = _require_remote(settings)
        catalog_map: dict[str, list[tuple[str, str]]] = context.application.bot_data.get(
            _BACKUP_CATALOG_KEY,
            {},
        )

        if (m := _CALLBACK_STACK.match(q.data)) is not None:
            await _handle_stack_button(q, context, remote, m.group(1))
            return

        if remote is None:
            await q.edit_message_text(
                "SSH к хосту Minecraft не настроен (см. MCOPS_SSH_* в env).",
                reply_markup=minecraft_menu_markup(),
            )
            return
        uid = u.id

        if (m := _CALLBACK_MC.match(q.data)) is not None:
            await _handle_minecraft_button(q, context, remote, m.group(1))
            return

        if (m := _CALLBACK_MANUAL.match(q.data)) is not None:
            await _handle_manual_backup_button(q, remote, m.group(1))
            return

        if (m := _CALLBACK_MANUAL_GO.match(q.data)) is not None:
            await _start_manual_backup_button(q, remote, m.group(1))
            return

        if (m := _CALLBACK_PICK.match(q.data)) is not None:
            token = m.group(1)
            idx = int(m.group(2))
            catalog = catalog_map.get(f"{uid}:{token}", [])
            if idx < 0 or idx >= len(catalog):
                await q.answer("Список устарел. Обновите /mc_backups.", show_alert=True)
                return
            eid, label = catalog[idx]
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Да, откатить", callback_data=f"mcy:{token}:{idx}"),
                        InlineKeyboardButton("Отмена", callback_data=f"mcn:{token}:{idx}"),
                    ],
                    [InlineKeyboardButton("Назад", callback_data="mc:backups")],
                    [InlineKeyboardButton("Домой", callback_data="nav:home")],
                ]
            )
            extra = label.strip() if label.strip() != eid.strip() else ""
            confirm_body = f"Подтвердите откат на:\n{eid}" + (f"\n{extra}" if extra else "")
            await q.edit_message_text(
                text=confirm_body,
                reply_markup=kb,
            )
            await _safe_answer_callback(q)
            return

        if (m := _CALLBACK_NO.match(q.data)) is not None:
            await q.edit_message_text(text="Откат отменён.", reply_markup=_backup_nav_markup())
            await _safe_answer_callback(q)
            return

        if (m := _CALLBACK_GO.match(q.data)) is not None:
            token = m.group(1)
            idx = int(m.group(2))
            catalog = catalog_map.get(f"{uid}:{token}", [])
            if idx < 0 or idx >= len(catalog):
                await q.answer("Список устарел.", show_alert=True)
                return
            eid, _label = catalog[idx]
            await q.edit_message_text(
                text=(
                    f"Запускаю restore для:\n{eid}\n\n"
                    "Буду обновлять это сообщение, пока команда выполняется."
                )
            )
            await _safe_answer_callback(q)
            await _run_restore_with_progress(q, remote, eid)
            return

    return handler


async def _append_watchdog_status(lines: list[str], remote: McopsRemoteSettings) -> None:
    """Append local VPS watchdog state to a stack status response."""

    code, out, err = await run_remote_mcops(remote, ["watchdog", "status", "--local", "--json"])
    lines.append(f"Watchdog: код {code}")
    if code != 0:
        lines.append(_tail_text(err or out, max_len=1200))
        return
    lines.append(_tail_text(out, max_len=1600))


def _stack_status_handler(settings: AppSettings) -> Handler:
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        u = update.effective_user
        if msg is None or not _is_allowed(u.id if u else None, settings):
            if msg:
                await msg.reply_text(_ACCESS_DENIED_RU)
            return
        regru = context.application.bot_data.get("regru")
        if not isinstance(regru, RegRuClient):
            await msg.reply_text("Внутренняя ошибка: нет RegRuClient.")
            return
        lines: list[str] = []
        try:
            payload = await regru.fetch_reglets()
            detail = None
            try:
                one = await regru.fetch_reglet()
                r = one.get("reglet") if isinstance(one, dict) else None
                if isinstance(r, dict):
                    detail = r
            except RegRuClientError:
                pass
            lines.append(
                format_reglet_telegram(
                    payload,
                    reglet_id=settings.reglet_id,
                    reglet_detail=detail,
                )
            )
        except RegRuClientError:
            lines.append("VPS: панель недоступна.")
        remote = _require_remote(settings)
        if remote is None:
            lines.append("Minecraft: SSH не настроен.")
        else:
            code, out, err = await run_remote_mcops(remote, ["status", "--json"])
            lines.append(f"Minecraft mcops status: код {code}")
            lines.append((out or err).strip()[:2500])
            await _append_watchdog_status(lines, remote)
        await msg.reply_text("\n\n".join(lines), reply_markup=_stack_menu_markup())

    return handler


def _stack_start_handler(settings: AppSettings) -> Handler:
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        u = update.effective_user
        if msg is None or not _is_allowed(u.id if u else None, settings):
            if msg:
                await msg.reply_text(_ACCESS_DENIED_RU)
            return
        remote = _require_remote(settings)
        if remote is None:
            await msg.reply_text("SSH к хосту Minecraft не настроен (см. MCOPS_SSH_* в env).")
            return
        regru = context.application.bot_data.get("regru")
        if not isinstance(regru, RegRuClient):
            await msg.reply_text("Внутренняя ошибка: нет RegRuClient.")
            return
        await msg.reply_text("Запрос на запуск VPS…")
        try:
            await regru.post_reglet_action(RegletAction.START)
        except RegRuClientError:
            await msg.reply_text("Панель недоступна при запуске VPS.")
            return
        await msg.reply_text("Жду SSH до хоста Minecraft (до ~5 минут)…")
        for _attempt in range(60):
            code, _out, _err = await run_remote_mcops(remote, ["status", "--json"])
            if code == 0:
                break
            await asyncio.sleep(5.0)
        else:
            await msg.reply_text("SSH так и не ответил. Проверьте сеть/VPS вручную.")
            return
        await msg.reply_text("SSH доступен. Запускаю Minecraft сервис…")
        code, out, err = await run_remote_mcops(remote, ["service", "start", "--local"])
        tail = _tail_text(out + "\n" + err, max_len=2500)
        await msg.reply_text(
            f"stack_start: service start код {code}\n{tail}",
            reply_markup=_stack_menu_markup(),
        )

    return handler


def _stack_stop_handler(settings: AppSettings) -> Handler:
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        u = update.effective_user
        if msg is None or not _is_allowed(u.id if u else None, settings):
            if msg:
                await msg.reply_text(_ACCESS_DENIED_RU)
            return
        remote = _require_remote(settings)
        if remote is None:
            await msg.reply_text("SSH к хосту Minecraft не настроен (см. MCOPS_SSH_* в env).")
            return
        if (context.args or []) != ["confirm"]:
            await msg.reply_text("Подтвердите остановку всего стека: /stack_stop confirm")
            return
        regru = context.application.bot_data.get("regru")
        if not isinstance(regru, RegRuClient):
            await msg.reply_text("Внутренняя ошибка: нет RegRuClient.")
            return
        await msg.reply_text("Останавливаю Minecraft…")
        code, out, err = await run_remote_mcops(remote, ["service", "stop", "--local"])
        tail = _tail_text(out + "\n" + err, max_len=2000)
        await msg.reply_text(f"Minecraft stop: код {code}\n{tail}")
        if code != 0:
            await msg.reply_text("VPS не останавливаю: Minecraft stop завершился с ошибкой.")
            return
        status_code, status_out, status_err = await run_remote_mcops(remote, ["status", "--json"])
        if status_code != 0:
            await msg.reply_text(
                "VPS не останавливаю: не удалось проверить статус Minecraft после stop.\n"
                f"{(status_err or status_out)[:1500]}"
            )
            return
        try:
            status_root = json.loads(status_out)
        except json.JSONDecodeError:
            await msg.reply_text("VPS не останавливаю: mcops status вернул не-JSON.")
            return
        if status_root.get("phase") != "stopped":
            await msg.reply_text(
                "VPS не останавливаю: Minecraft не выглядит остановленным "
                f"(phase={status_root.get('phase')})."
            )
            return
        await msg.reply_text("Запрос на остановку VPS…")
        try:
            await regru.post_reglet_action(RegletAction.STOP)
        except RegRuClientError:
            await msg.reply_text("Панель недоступна при остановке VPS.")
            return
        await msg.reply_text(
            "Запрос на остановку VPS отправлен.",
            reply_markup=_stack_menu_markup(),
        )

    return handler
