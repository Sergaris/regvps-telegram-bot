"""Утилиты для inline-клавиатур Telegram.

Ширина области клавиатуры привязана к ширине текста сообщения над ней: короткий
текст даёт узкую полосу кнопок и визуально разную ширину колонок. Дополняем текст
сообщения невидимым padding (U+00A0 + U+200D), не меняя подписи кнопок.
"""

import unicodedata

from telegram import InlineKeyboardMarkup

_PADDING_NBSP = "\u00a0"
_INVISIBLE_TAIL = "\u200d"


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
    """Минимальная визуальная ширина текста сообщения для ровной сетки клавиатуры.

    Берётся максимум по рядам суммы визуальных ширин подписей кнопок в ряду —
    грубая оценка «естественной» ширины клавиатуры по содержимому.

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
    """Расширяет текст сообщения, чтобы полоса кнопок не была уже текста.

    Подписи кнопок не изменяются. Padding добавляется в конец последней строки
    сообщения (или в одну строку, если текст пустой).

    Args:
        text: Текст сообщения, как у пользователя.
        markup: Клавиатура под сообщением; при ``None`` возвращается ``text`` без изменений.

    Returns:
        Текст с невидимым хвостом при необходимости.
    """

    if markup is None or not markup.inline_keyboard:
        return text
    needed = markup_min_message_visual_width(markup)
    if needed <= 0:
        return text
    lines = text.split("\n")
    max_line_w = max((visual_text_width(line) for line in lines), default=0)
    gap = needed - max_line_w
    if gap <= 0:
        return text
    tail_w = visual_text_width(_INVISIBLE_TAIL)
    nb = gap - tail_w
    if nb < 0:
        nb = 0
    pad = _PADDING_NBSP * nb + _INVISIBLE_TAIL
    if not lines:
        return pad
    lines[-1] = lines[-1] + pad
    return "\n".join(lines)
