"""Tests for Minecraft Telegram helper formatting."""

from vps_telegram_bot.minecraft_handlers import _manual_slot_labels, minecraft_menu_markup


def test_manual_slot_labels_show_occupied_and_empty_slots() -> None:
    rows = [
        {
            "id": "tar:worlds-manual-manual-1-20260425010000.tar.gz",
            "mtime": 1_777_077_600.0,
        },
        {
            "id": "tar:worlds-manual-manual-3-20260425020000.tar.gz",
            "mtime": 1_777_081_200.0,
        },
        {
            "id": "mod:world_2026-04-25_03-28-14.zip",
            "mtime": 1_777_085_000.0,
        },
    ]

    labels = _manual_slot_labels(rows)

    assert labels["manual-1"].startswith("manual-1: занят")
    assert labels["manual-2"] == "manual-2: пусто"
    assert labels["manual-3"].startswith("manual-3: занят")


def test_manual_slot_labels_prefer_manual_slot_status_rows() -> None:
    rows = [
        {"slot": "manual-1", "occupied": False},
        {"slot": "manual-2", "occupied": True, "mtime": 1_777_077_600.0},
        {"slot": "manual-3", "occupied": False},
    ]

    labels = _manual_slot_labels(rows)

    assert labels["manual-1"] == "manual-1: пусто"
    assert labels["manual-2"].startswith("manual-2: занят")
    assert labels["manual-3"] == "manual-3: пусто"


def test_minecraft_menu_markup_includes_modrinth_callbacks() -> None:
    """Главное меню Minecraft содержит кнопки плана и подтверждения apply Modrinth."""

    markup = minecraft_menu_markup()
    flat: list[str] = []
    for row in markup.inline_keyboard:
        for btn in row:
            flat.append(str(btn.callback_data))
    assert "mc:mods_plan" in flat
    assert "mc:confirm_mods_apply" in flat
