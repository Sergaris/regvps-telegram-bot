"""Тесты padding текста сообщения под ширину inline-клавиатуры."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from vps_telegram_bot.telegram_inline_kb import (
    UNIFIED_INLINE_MENU_MIN_VISUAL_WIDTH,
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


def test_pad_message_unified_width_for_narrow_keyboard() -> None:
    """Узкая клавиатура (VPS+Minecraft) — текст всё равно расширяется до общего минимума."""

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
    assert visual_text_width(out) >= UNIFIED_INLINE_MENU_MIN_VISUAL_WIDTH


def test_pad_message_respects_wider_dynamic_keyboard() -> None:
    """Если клавиатура шире общего минимума (например длинные подписи в списке), берётся она."""

    long_label = "x" * 50
    mk = InlineKeyboardMarkup([[InlineKeyboardButton(long_label, callback_data="z")]])
    needed = markup_min_message_visual_width(mk)
    assert needed > UNIFIED_INLINE_MENU_MIN_VISUAL_WIDTH
    out = pad_message_for_inline_keyboard("a", mk)
    assert visual_text_width(out) >= needed


def test_pad_message_pads_widest_line_when_multiline() -> None:
    """При нескольких строках padding к самой широкой строке, иначе пузырь не расширится."""

    mk = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("VPS", callback_data="a"),
                InlineKeyboardButton("Minecraft", callback_data="b"),
            ],
        ]
    )
    text = "Коротко\n" + "Очень длинная строка которая уже почти на всю ширину экрана."
    out = pad_message_for_inline_keyboard(text, mk)
    lines = out.split("\n")
    assert len(lines) == 2
    assert visual_text_width(lines[0]) == visual_text_width("Коротко")
    assert visual_text_width(lines[1]) >= UNIFIED_INLINE_MENU_MIN_VISUAL_WIDTH


def test_pad_message_no_markup_returns_unchanged() -> None:
    assert pad_message_for_inline_keyboard("x", None) == "x"
