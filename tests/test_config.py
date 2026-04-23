"""Валидация `from_environ` с подменой словаря."""

import pytest

from vps_telegram_bot.config import from_environ


def test_from_environ_minimal() -> None:
    """Минимально корректный набор → `AppSettings` с id и allowlist."""
    s = from_environ(
        {
            "REGRU_CLOUDVPS_TOKEN": "a",
            "REGRU_REGLET_ID": "1",
            "TELEGRAM_BOT_TOKEN": "b",
            "TELEGRAM_ALLOWED_USER_IDS": "100, 200",
        }
    )
    assert s.regru_token == "a"
    assert s.reglet_id == 1
    assert s.telegram_bot_token == "b"
    assert s.allowed_telegram_user_ids == frozenset({100, 200})
    assert "v1" in s.regru_api_base or s.regru_api_base.endswith("v1")


def test_from_environ_missing_allowlist_raises() -> None:
    """Пустой allowlist → `ValueError`."""
    with pytest.raises(ValueError, match="TELEGRAM_ALLOWED_USER_IDS"):
        from_environ(
            {
                "REGRU_CLOUDVPS_TOKEN": "a",
                "REGRU_REGLET_ID": "1",
                "TELEGRAM_BOT_TOKEN": "b",
                "TELEGRAM_ALLOWED_USER_IDS": "",
            }
        )
