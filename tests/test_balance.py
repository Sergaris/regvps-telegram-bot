"""Парсинг `GET /v1/balance_data`."""

from vps_telegram_bot.regru_client import format_balance_telegram


def test_format_balance_num() -> None:
    t = format_balance_telegram({"balance_data": {"balance": 1234.5}})
    assert "1 234" in t
    assert "₽" in t
    assert "Баланс" in t


def test_format_balance_string() -> None:
    t = format_balance_telegram({"balance_data": {"balance": "99.99"}})
    assert "99" in t
    assert "₽" in t


def test_format_balance_missing() -> None:
    t = format_balance_telegram({})
    assert "balance_data" in t or "не вернула" in t
