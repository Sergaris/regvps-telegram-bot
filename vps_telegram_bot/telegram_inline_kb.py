"""Утилиты для inline-клавиатур Telegram.

У клиента Telegram ширина кнопки в ряду задаётся содержимым текста; API не даёт
фиксировать пиксели. Выравниваем подписи по «визуальной ширине» (East_Asian_Width)
и дополняем короткие строки неразрывными пробелами (U+00A0), в конец добавляем
невидимый U+200D, чтобы хвост не обрезался при отрисовке.
"""

import unicodedata
from collections.abc import Sequence

from telegram import InlineKeyboardButton

# Неразрывный пробел — ширина как у обычного пробела, Telegram не схлопывает подряд.
_PADDING_NBSP = "\u00a0"
# Zero-width joiner: невидимый «якорь» в конце padding (распространённый приём).
_INVISIBLE_TAIL = "\u200d"


def visual_text_width(text: str) -> int:
    """Суммарная «ширина» строки по East_Asian_Width (узкий=1, широкий=2).

    Args:
        text: Произвольная Unicode-строка.

    Returns:
        Целое число для сравнения длин подписей кнопок в одном ряду.
    """

    total = 0
    for ch in text:
        eaw = unicodedata.east_asian_width(ch)
        if eaw in {"F", "W"}:
            total += 2
        else:
            total += 1
    return total


def pad_inline_button_labels_to_equal_width(labels: Sequence[str]) -> list[str]:
    """Дополняет подписи в одном ряду до одинаковой визуальной ширины.

    Args:
        labels: Тексты кнопок в одной строке клавиатуры.

    Returns:
        Список строк той же длины; у коротких в конец добавлен padding и U+200D.
    """

    items = list(labels)
    if len(items) <= 1:
        return items
    max_w = max(visual_text_width(s) for s in items)
    tail_w = visual_text_width(_INVISIBLE_TAIL)
    out: list[str] = []
    for s in items:
        gap = max_w - visual_text_width(s)
        if gap <= 0:
            out.append(s)
            continue
        nb = gap - tail_w
        if nb < 0:
            nb = 0
        out.append(s + _PADDING_NBSP * nb + _INVISIBLE_TAIL)
    return out


def equal_width_inline_row(buttons: Sequence[InlineKeyboardButton]) -> list[InlineKeyboardButton]:
    """Возвращает копии кнопок ряда с выровненным полем ``text``.

    Args:
        buttons: От одной кнопки и больше; при одной кнопке возвращается тот же список.

    Returns:
        Новые ``InlineKeyboardButton`` с теми же ``callback_data`` и новым ``text``.
    """

    btn_list = list(buttons)
    if len(btn_list) <= 1:
        return btn_list
    padded = pad_inline_button_labels_to_equal_width([b.text for b in btn_list])
    return [
        InlineKeyboardButton(text=new_text, callback_data=b.callback_data)
        for new_text, b in zip(padded, btn_list, strict=True)
    ]
