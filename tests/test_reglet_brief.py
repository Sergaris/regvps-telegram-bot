"""Сводка по `GET /reglets` (укороченный JSON из API)."""

from vps_telegram_bot.reglet_brief import (
    format_reglet_telegram,
    reglet_is_running_from_list_payload,
)

_MINIMAL: dict = {
    "reglets": [
        {
            "id": 7027955,
            "name": "Minecraft_Create",
            "status": "active",
            "ip": "130.49.148.15",
            "region_slug": "openstack-msk2",
            "memory": 16384,
            "disk": 40,
            "disk_usage": 0.0,
            "vcpus": 6,
            "locked": 1,
            "is_blocked": 1,
            "blocks": ["block_smtp"],
            "billed_until": "2026-04-24 00:46:08",
            "image": {
                "name": "Minecraf with mods",
                "distribution": "ubuntu-24.04",
            },
            "size": {
                "name": "High C6-M16-D40",
                "vcpus": 6,
            },
        }
    ],
    "links": {
        "actions": [
            {
                "id": "chain_19295705",
                "resource_id": 7027955,
                "resource_type": "reglet",
                "type": "StopServerUseCase",
                "status": "in-progress",
                "created_at": "2026-04-24 00:46:58",
            }
        ]
    },
}


def test_format_reglet_telegram_includes_essentials() -> None:
    t = format_reglet_telegram(_MINIMAL, reglet_id=7027955)
    assert "Minecraft_Create" in t
    assert "130.49.148.15" in t
    assert "active" in t
    assert "6 vCPU" in t or "6 v" in t
    assert "StopServerUseCase" in t
    assert "in-progress" in t
    assert "остановка" in t
    assert "Сервис:" in t
    assert "SMTP" in t
    assert "16" in t and "ГБ" in t
    assert "7027955" in t
    assert "панель:" in t


def test_format_reglet_missing() -> None:
    t = format_reglet_telegram({"reglets": []}, reglet_id=1)
    assert "не" in t.lower() or "нет" in t.lower() or "id = 1" in t


def test_reglet_is_running_active_vs_off() -> None:
    assert reglet_is_running_from_list_payload(_MINIMAL, reglet_id=7027955) is True
    off = {
        "reglets": [
            {
                "id": 7027955,
                "name": "T",
                "status": "off",
                "region_slug": "r",
                "memory": 1024,
                "disk": 40,
                "vcpus": 1,
                "image": {"name": "i", "distribution": "u"},
            }
        ],
        "links": {"actions": []},
    }
    assert reglet_is_running_from_list_payload(off, reglet_id=7027955) is False


def test_reglet_is_running_unknown_when_missing() -> None:
    assert reglet_is_running_from_list_payload({"reglets": []}, reglet_id=7027955) is None
    assert reglet_is_running_from_list_payload({}, reglet_id=1) is None


def test_detail_merges_disk_usage_gb() -> None:
    """`disk_usage` в списке 0, в `GET /id` — 6.7 ГБ: показываем гигабайты, не %."""
    pl = {
        "reglets": [
            {
                "id": 7027955,
                "name": "T",
                "status": "active",
                "region_slug": "r",
                "memory": 1024,
                "disk": 40,
                "disk_usage": 0.0,
                "vcpus": 1,
                "image": {"name": "i", "distribution": "u"},
            }
        ],
        "links": {"actions": []},
    }
    t = format_reglet_telegram(
        pl,
        reglet_id=7027955,
        reglet_detail={"disk": 40, "disk_usage": 6.7},
    )
    assert "6.7" in t
    assert "40" in t
    assert "занято" in t
    assert "%" in t
