"""Telegram handlers for remote ``mcops`` and stack control."""

import asyncio
import json
import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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


Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


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
        await msg.reply_text(out.strip()[:3900])

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
        tail = (out + "\n" + err).strip()[:3500]
        await msg.reply_text(f"Код {code}\n{tail}")

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
        await msg.reply_text(f"Игроков онлайн (по RCON list): {out.strip()}")

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
            buttons.append([InlineKeyboardButton(f"↩ {label}", callback_data=f"mcs:{token}:{idx}")])
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
            await msg.reply_text("Использование: /mc_backup_manual manual-1|manual-2|manual-3")
            return
        slot = args[0]
        await msg.reply_text(f"Запускаю ручной бэкап слота {slot}…")
        code, out, err = await run_remote_mcops(
            remote,
            ["backup", "create", "--slot", slot, "--local"],
        )
        tail = (out + "\n" + err).strip()[:3500]
        await msg.reply_text(f"Код {code}\n{tail}")

    return handler


def _minecraft_callback_router(settings: AppSettings) -> Handler:
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None or q.data is None:
            return
        u = q.from_user
        if not _is_allowed(u.id if u else None, settings):
            await q.answer("Нет доступа.", show_alert=True)
            return
        remote = _require_remote(settings)
        if remote is None:
            await q.answer("SSH не настроен.", show_alert=True)
            return
        uid = u.id
        catalog_map: dict[str, list[tuple[str, str]]] = context.application.bot_data.get(
            _BACKUP_CATALOG_KEY,
            {},
        )

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
                    ]
                ]
            )
            extra = label.strip() if label.strip() != eid.strip() else ""
            confirm_body = f"Подтвердите откат на:\n{eid}" + (f"\n{extra}" if extra else "")
            await q.edit_message_text(
                text=confirm_body,
                reply_markup=kb,
            )
            await q.answer()
            return

        if (m := _CALLBACK_NO.match(q.data)) is not None:
            await q.edit_message_text(text="Откат отменён.")
            await q.answer()
            return

        if (m := _CALLBACK_GO.match(q.data)) is not None:
            token = m.group(1)
            idx = int(m.group(2))
            catalog = catalog_map.get(f"{uid}:{token}", [])
            if idx < 0 or idx >= len(catalog):
                await q.answer("Список устарел.", show_alert=True)
                return
            eid, _label = catalog[idx]
            await q.edit_message_text(text=f"Запускаю restore для:\n{eid}")
            await q.answer()
            code, out, err = await run_remote_mcops(
                remote,
                [
                    "backup",
                    "restore",
                    eid,
                    "--local",
                    "--confirm-destructive",
                ],
            )
            tail = (out + "\n" + err).strip()[:3500]
            if q.message:
                await q.message.reply_text(f"Код {code}\n{tail}")
            return

    return handler


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
        await msg.reply_text("\n\n".join(lines))

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
        tail = (out + "\n" + err).strip()[:2500]
        await msg.reply_text(f"stack_start: service start код {code}\n{tail}")

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
        tail = (out + "\n" + err).strip()[:2000]
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
        await msg.reply_text("Запрос на остановку VPS отправлен.")

    return handler
