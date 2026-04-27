"""Тесты padding текста сообщения под ширину inline-клавиатуры."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from vps_telegram_bot.telegram_inline_kb import (
    markup_min_message_visual_width,
    pad_message_for_inline_keyboard,
    visual_text_width,
)


def test_visual_text_width_counts_wide_chars() -> None:
    assert visual_text_width("VPS") == 3
    assert visual_text_width("日") == 2
    assert visual_text_width("a日b") == 4


def test_markup_min_width_sums_row() -> None:
    mk = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("VPS", callback_data="a"),
                InlineKeyboardButton("Minecraft", callback_data="b"),
            ],
        ]
    )
    assert markup_min_message_visual_width(mk) == visual_text_width("VPS") + visual_text_width(
        "Minecraft"
    )


def test_pad_message_extends_short_body_for_keyboard() -> None:
    mk = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("VPS", callback_data="a"),
                InlineKeyboardButton("Minecraft", callback_data="b"),
            ],
        ]
    )
    out = pad_message_for_inline_keyboard("Hi", mk)
    assert out.startswith("Hi")
    assert visual_text_width(out) >= markup_min_message_visual_width(mk)


def test_pad_message_no_markup_returns_unchanged() -> None:
    assert pad_message_for_inline_keyboard("x", None) == "x"
