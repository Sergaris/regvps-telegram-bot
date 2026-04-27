"""Тесты выравнивания подписей inline-кнопок."""

from telegram import InlineKeyboardButton

from vps_telegram_bot.telegram_inline_kb import (
    equal_width_inline_row,
    pad_inline_button_labels_to_equal_width,
    visual_text_width,
)


def test_visual_text_width_counts_wide_chars() -> None:
    assert visual_text_width("VPS") == 3
    assert visual_text_width("日") == 2
    assert visual_text_width("a日b") == 4


def test_pad_inline_button_labels_equalizes_visual_width() -> None:
    out = pad_inline_button_labels_to_equal_width(["VPS", "Minecraft"])
    assert visual_text_width(out[0]) == visual_text_width(out[1])
    assert out[0].startswith("VPS")
    assert out[1] == "Minecraft"


def test_equal_width_inline_row_preserves_callback_data() -> None:
    row = equal_width_inline_row(
        [
            InlineKeyboardButton("A", callback_data="x:1"),
            InlineKeyboardButton("BB", callback_data="x:2"),
        ]
    )
    assert [b.callback_data for b in row] == ["x:1", "x:2"]
    assert visual_text_width(row[0].text) == visual_text_width(row[1].text)
