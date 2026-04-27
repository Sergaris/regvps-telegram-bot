"""Утилиты для inline-клавиатур Telegram.

Ширина полосы inline-клавиатуры следует за шириной текста сообщения над ней.
Дополняем текст невидимым padding (U+00A0 + U+200D), не меняя подписи кнопок.

Чтобы все экраны с меню выглядели одной ширины, используется общий пол
``UNIFIED_INLINE_MENU_MIN_VISUAL_WIDTH``: минимальная визуальная ширина текста
не ниже этого значения и не ниже оценки по текущей клавиатуре.
"""

import unicodedata

from telegram import InlineKeyboardMarkup

_PADDING_NBSP = "\u00a0"
_INVISIBLE_TAIL = "\u200d"

# Самый широкий типовой ряд двух кнопок в боте (EAW): «Да, новый мир…» + «Отмена».
# Динамические списки бэкапов могут быть шире — тогда сработает max с разметкой.
UNIFIED_INLINE_MENU_MIN_VISUAL_WIDTH = 35


def visual_text_width(text: str) -> int:
    """Суммарная «ширина» строки по East_Asian_Width (узкий=1, широкий=2).

    Args:
        text: Произвольная Unicode-строка.

    Returns:
        Целое число для сравнения длин строк.
    """

    total = 0
    for ch in text:
        eaw = unicodedata.east_asian_width(ch)
        if eaw in {"F", "W"}:
            total += 2
        else:
            total += 1
    return total


def markup_min_message_visual_width(markup: InlineKeyboardMarkup) -> int:
    """Минимальная визуальная ширина текста для ровной сетки данной клавиатуры.

    Берётся максимум по рядам суммы визуальных ширин подписей кнопок в ряду.

    Args:
        markup: Разметка inline-клавиатуры.

    Returns:
        Ненотрицательное целое; для пустой клавиатуры 0.
    """

    max_sum = 0
    for row in markup.inline_keyboard:
        row_sum = sum(visual_text_width(btn.text) for btn in row)
        if row_sum > max_sum:
            max_sum = row_sum
    return max_sum


def pad_message_for_inline_keyboard(
    text: str,
    markup: InlineKeyboardMarkup | None,
) -> str:
    """Расширяет текст сообщения под единую ширину меню и под текущую клавиатуру.

    Подписи кнопок не изменяются. Невидимый padding дописывается к **самой
    длинной** строке текста (по визуальной ширине), чтобы ширина пузыря
    выросла корректно и при нескольких строках.

    Args:
        text: Текст сообщения, как у пользователя.
        markup: Клавиатура под сообщением; при ``None`` возвращается ``text`` без изменений.

    Returns:
        Текст с невидимым хвостом при необходимости.
    """

    if markup is None or not markup.inline_keyboard:
        return text
    from_markup = markup_min_message_visual_width(markup)
    needed = max(UNIFIED_INLINE_MENU_MIN_VISUAL_WIDTH, from_markup)
    if needed <= 0:
        return text
    lines = text.split("\n")
    if not lines:
        lines = [""]
    widths = [visual_text_width(line) for line in lines]
    max_w = max(widths)
    gap = needed - max_w
    if gap <= 0:
        return text
    tail_w = visual_text_width(_INVISIBLE_TAIL)
    nb = gap - tail_w
    if nb < 0:
        nb = 0
    pad = _PADDING_NBSP * nb + _INVISIBLE_TAIL
    # Паддим самую широкую строку (последнюю при равенстве — стабильнее для UX).
    best_i = len(lines) - 1
    for i, w in enumerate(widths):
        if w >= widths[best_i]:
            best_i = i
    lines[best_i] = lines[best_i] + pad
    return "\n".join(lines)
