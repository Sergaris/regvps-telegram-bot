"""Tests for Minecraft Telegram helper formatting."""

from vps_telegram_bot.minecraft_handlers import (
    _manual_slot_labels,
    _mcops_level_seed_unsupported_hint,
    _world_reset_argv_for_telegram,
    admin_menu_markup,
    minecraft_menu_markup,
)


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


def test_minecraft_menu_markup_matches_compact_layout() -> None:
    """Вкладка Minecraft: перезапуск, бэкапы, ручной бэкап, назад (без модов)."""

    markup = minecraft_menu_markup()
    flat: list[str] = []
    for row in markup.inline_keyboard:
        for btn in row:
            flat.append(str(btn.callback_data))
    assert flat == [
        "mc:confirm_restart",
        "mc:backups",
        "mc:manual_menu",
        "nav:home",
    ]


def test_admin_menu_markup_mods_row_third_after_balance() -> None:
    """Админ-панель: ряд 3 — две кнопки модов под полной строкой баланса."""

    markup = admin_menu_markup()
    rows = [[str(b.callback_data) for b in row] for row in markup.inline_keyboard]
    assert rows[0] == ["adm:vps_status", "adm:mc_status"]
    assert rows[1] == ["adm:vps_balance"]
    assert rows[2] == ["adm:mods_plan", "adm:confirm_mods_apply"]
    assert rows[3] == ["adm:backup_delete_menu"]
    assert rows[4] == ["adm:world_regen_menu"]
    assert rows[5] == ["nav:home"]


def test_world_reset_argv_random_vs_fixed() -> None:
    """Пустой seed → только флаг --level-seed; непустой → значение для mcops."""

    assert _world_reset_argv_for_telegram(seed=None) == [
        "world",
        "reset",
        "--no-backup",
        "--local",
        "--level-seed",
    ]
    assert _world_reset_argv_for_telegram(seed="") == [
        "world",
        "reset",
        "--no-backup",
        "--local",
        "--level-seed",
    ]
    assert _world_reset_argv_for_telegram(seed=" 42 ") == [
        "world",
        "reset",
        "--no-backup",
        "--local",
        "--level-seed",
        "42",
    ]


def test_mcops_level_seed_unsupported_hint_detects_argparse() -> None:
    err = "cli.py: error: unrecognized arguments: --level-seed 123"
    assert "mcops" in _mcops_level_seed_unsupported_hint(err).lower()
    assert _mcops_level_seed_unsupported_hint("ok") == ""
