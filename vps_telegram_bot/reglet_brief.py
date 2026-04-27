"""Краткий текст о reglet по ответу `GET /v1/reglets`."""

import logging
from collections.abc import Mapping
from typing import Any, cast

log = logging.getLogger(__name__)

_MAX_TG = 4000  # оставим запас до лимита Telegram 4096

_DETAIL_KEYS_DISK = ("disk", "disk_usage", "billed_until")


def _merge_detail_into_reglet(
    from_list: dict[str, Any],
    detail: Mapping[str, Any],
) -> dict[str, Any]:
    """Слить `GET /reglets/{id}`: актуальные `disk` / `disk_usage` (в ГБ)."""
    out: dict[str, Any] = {**from_list}
    for k in _DETAIL_KEYS_DISK:
        if k in detail:
            out[k] = detail[k]
    return out


def format_reglet_telegram(
    api_payload: Mapping[str, Any],
    *,
    reglet_id: int,
    reglet_detail: Mapping[str, Any] | None = None,
) -> str:
    """Собрать короткую сводку по `reglet` с заданным `id` (как `REGRU_REGLET_ID`).

    Args:
        api_payload: JSON от `GET /v1/reglets` (список, есть `links` и операции).
        reglet_id: id виртуалки из настроек.
        reglet_detail: `GET /v1/reglets/{id}`: занятость `disk_usage` в ГБ по доке Reg.ru;
            в ответе списка `disk_usage` нередко 0.0.

    Returns:
        Многострочный текст в UTF-8.
    """
    raw = api_payload.get("reglets")
    if not isinstance(raw, list):
        return "Панель вернула неожиданный ответ: нет поля `reglets` (список)."

    found: dict[str, Any] | None = None
    for item in raw:
        if not isinstance(item, dict):
            continue
        if item.get("id") == reglet_id:
            found = cast("dict[str, Any]", item)
            break
    if found is None:
        return f"В ответе нет reglet с id = {reglet_id}."

    d: dict[str, Any] = {**found}
    if reglet_detail is not None:
        d = _merge_detail_into_reglet(d, reglet_detail)
    name = str(d.get("name") or "—")
    st = str(d.get("status") or "—")
    sub = d.get("sub_status")
    sub_s = f" / {sub}" if sub is not None and sub != "" else ""

    primary = _select_action_for_reglet(api_payload, reglet_id)
    status_explain = _in_progress_explain(st, sub_s, primary)

    ip = str(d.get("ip") or "—")
    ipv6 = d.get("ipv6")
    if ipv6 is not None and str(ipv6) != "":
        ip = f"{ip} (v6: {ipv6})"
    region = str(d.get("region_slug") or "—")

    sz = d.get("size")
    size_vcpus = sz.get("vcpus") if isinstance(sz, dict) else None
    vcpus = _coerce_int(d.get("vcpus"), size_vcpus)
    mem = _as_ram(d.get("memory"))
    dsk = _as_disk_gb(d.get("disk"))
    du = d.get("disk_usage")
    disk_line = _disk_line(du, d.get("disk"))

    if isinstance(sz, dict):
        tier = str(sz.get("name") or "—")
    else:
        tier = "—"
    v_line = f"{vcpus} vCPU" if vcpus is not None else "vCPU: —"
    res_line = f"{v_line}, {mem} RAM, {dsk} диск, тариф: {tier}"

    img = d.get("image")
    if isinstance(img, dict):
        iname = str(img.get("name") or "—")
        dist = str(img.get("distribution") or "—")
        image_line = f"Образ: {iname} ({dist})"
    else:
        image_line = "Образ: —"

    flags = _format_service_flags(d)

    billed = d.get("billed_until")
    bl = f"Биллинг: {billed}\n" if billed is not None and str(billed) != "" else ""

    op_line = _format_latest_action(primary)

    lines: list[str] = [
        f"«{name}»  id {reglet_id}",
        f"Статус: {st}{sub_s}{status_explain}".rstrip(),
        f"IP: {ip}",
        f"Регион: {region}",
        res_line,
    ]
    if flags:
        lines.append(f"Сервис: {flags}")
    lines.append(disk_line)
    lines.append(image_line)
    if bl:
        lines.append(bl.rstrip())
    if op_line:
        lines.append(op_line)

    out = "\n".join(lines)
    if len(out) > _MAX_TG:
        log.warning("reglet_brief: truncated from %d chars", len(out))
        return out[:_MAX_TG] + "…"
    return out.rstrip()


def reglet_panel_action_in_progress_from_list_payload(
    api_payload: Mapping[str, Any],
    *,
    reglet_id: int,
) -> bool:
    """По ``links.actions`` панели: для этого reglet есть операция со статусом ``in-progress``.

    Reg.ru отдаёт ``in-progress`` при запуске, остановке, перезагрузке и других действиях.
    Пока операция не завершена, ``status`` reglet может ещё не отражать целевое состояние —
    UI не должен трактовать это как «VPS точно выключен» или дублировать те же действия.

    Args:
        api_payload: JSON корня списка reglets (с полем ``links``).
        reglet_id: Идентификатор reglet из настроек.

    Returns:
        ``True``, если для данного ``resource_id`` / ``resource_type`` == ``reglet`` найдена
        любая запись в ``actions`` со статусом ``in-progress``; иначе ``False``.
    """
    links = api_payload.get("links")
    if not isinstance(links, dict):
        return False
    acts = links.get("actions")
    if not isinstance(acts, list):
        return False
    for a in acts:
        if not isinstance(a, dict):
            continue
        if a.get("resource_id") != reglet_id:
            continue
        if str(a.get("resource_type") or "") != "reglet":
            continue
        if str(a.get("status") or "").strip().lower() == "in-progress":
            return True
    return False


def reglet_start_in_progress_from_list_payload(
    api_payload: Mapping[str, Any],
    *,
    reglet_id: int,
) -> bool:
    """Устаревшее имя: то же, что ``reglet_panel_action_in_progress_from_list_payload``."""

    return reglet_panel_action_in_progress_from_list_payload(
        api_payload,
        reglet_id=reglet_id,
    )


def reglet_is_running_from_list_payload(
    api_payload: Mapping[str, Any],
    *,
    reglet_id: int,
) -> bool | None:
    """По ответу ``GET /v1/reglets`` понять, включена ли виртуалка (доступна по панели).

    Считаем «включённой» только ``status == active`` (как в типичном ответе Reg.ru CloudVPS).
    При отсутствии reglet в списке или неверной структуре возвращаем ``None`` (неизвестно).

    Args:
        api_payload: JSON корня списка reglets.
        reglet_id: Идентификатор reglet из настроек.

    Returns:
        ``True`` если статус ``active``, ``False`` если reglet найден и статус иной,
        ``None`` если reglet не найден или нет поля ``reglets``-списка.
    """
    raw = api_payload.get("reglets")
    if not isinstance(raw, list):
        return None
    for item in raw:
        if not isinstance(item, dict):
            continue
        if item.get("id") != reglet_id:
            continue
        st = str(item.get("status") or "").strip().lower()
        return st == "active"
    return None


def _truthy(x: object) -> bool:
    if x in (None, 0, "0", False, ""):
        return False
    return bool(x)


def _select_action_for_reglet(
    api_payload: Mapping[str, Any],
    resource_id: int,
) -> dict[str, Any] | None:
    """Самая релевантная запись `links.actions` к этому reglet, иначе первая."""
    links = api_payload.get("links")
    if not isinstance(links, dict):
        return None
    acts = links.get("actions")
    if not isinstance(acts, list) or not acts:
        return None
    for a in acts:
        if not isinstance(a, dict):
            continue
        if a.get("resource_id") == resource_id and a.get("resource_type") == "reglet":
            return cast("dict[str, Any]", a)
    first = acts[0]
    if isinstance(first, dict):
        return cast("dict[str, Any]", first)
    return None


def _russian_action_label(raw_type: str) -> str:
    """Понятное имя вместо `*UseCase` из панели."""
    m: dict[str, str] = {
        "StartServerUseCase": "запуск",
        "StopServerUseCase": "остановка",
        "RebootServerUseCase": "перезагрузка",
    }
    if raw_type in m:
        return m[raw_type]
    if "UseCase" in raw_type:
        return raw_type.replace("ServerUseCase", " VM").replace("UseCase", "")
    return raw_type


def _in_progress_explain(
    st: str,
    sub_s: str,
    primary: dict[str, Any] | None,
) -> str:
    """Подсказка, когда поле `status` ещё `active`/`off`, а операция в `links` ещё in-progress."""
    if not primary or str(primary.get("status") or "").lower() != "in-progress":
        return ""
    rus = _russian_action_label(str(primary.get("type") or "операция"))
    return f"  |  панель: {rus} (in-progress) · из API: «{st}»{sub_s}"


def _disk_line(used: object, total: object) -> str:
    """`disk` и `disk_usage` в Reg.ru: оба **гигабайты** (занято и размер), не %.

    См. [доку: info](https://developers.cloudvps.reg.ru/reglets/info.html) —
    `disk_usage` = «фактический размер диска, ГБ». Список `/v1/reglets` часто даёт
    `disk_usage: 0.0`, пока нет `GET /v1/reglets/{id}`.
    """
    d_total = _float_none(total)
    d_used = _float_none(used)
    if d_total is not None and d_total > 0.0 and d_used is not None and d_used >= 0.0:
        pct = 100.0 * d_used / d_total
        return f"Диск: {d_used:g} ГБ занято из {d_total:g} ГБ (≈{pct:.0f} %)"
    if d_used is not None and d_used >= 0.0:
        rest = f" · всего {d_total:g} ГБ" if d_total and d_total > 0.0 else ""
        return f"Диск: занято {d_used:g} ГБ{rest} (API в ГБ, не в %.)"
    if d_total is not None and d_total > 0.0:
        return (
            f"Диск: размер {d_total:g} ГБ, занятые ГБ не в ответе. "
            "По Доке Reg.ru точнее `GET /reglets/{id}`; бот для /vps_info вызывает его."
        )

    return "Диск: —"


def _float_none(x: object) -> float | None:
    if x is None:
        return None
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        f = float(x)
        if f < 0.0 or f > 1e12:
            return None
        return f
    if isinstance(x, str):
        t = x.strip()
        if not t or t in ("—", "null", "none", "N/A", "n/a"):
            return None
        try:
            f = float(t)
        except ValueError:
            return None
        if f < 0.0 or f > 1e12:
            return None
        return f
    return None


def _format_service_flags(d: Mapping[str, Any]) -> str:
    """`locked`, `blocks[]`, `is_blocked` в человекочитаемом виде."""
    parts: list[str] = []
    if _truthy(d.get("locked")):
        parts.append("панель держит lock на изменения (часто на время смены состояния)")

    blocks = d.get("blocks")
    if isinstance(blocks, list):
        for b in blocks:
            if not isinstance(b, str):
                continue
            if b == "block_smtp":
                parts.append("запрет исходящего SMTP")
            else:
                parts.append(b)
    if not parts and _truthy(d.get("is_blocked")):
        parts.append("флаг ограничений (см. панель)")

    return " · ".join(parts)


def _format_latest_action(
    primary: dict[str, Any] | None,
) -> str:
    """Полные поля `links.actions[…]` (тип, статус, время)."""
    if not primary:
        return ""
    raw_t = str(primary.get("type") or "—")
    rus = _russian_action_label(raw_t)
    s = str(primary.get("status") or "—")
    c = primary.get("created_at")
    if c is not None:
        return f"Панель: {rus} — {s} · {raw_t} · {c}."
    return f"Панель: {rus} — {s} · {raw_t}."


def _as_ram(n: object) -> str:
    """`memory` в Reg.ru: значения 1024+ — мегабайты (16384 -> 16 ГБ); малые — гигабайты."""
    if n is None:
        return "—"
    if isinstance(n, bool):
        return "—"
    if not isinstance(n, (int, float)):
        return str(n)
    f = float(n)
    if f < 0.0:
        return "—"
    if f >= 1024.0:
        g = f / 1024.0
        s = f"{g:.0f} ГБ" if abs(g - round(g)) < 0.05 else f"{g:.1f} ГБ"
        return s
    if f <= 256.0:
        return f"{f:.0f} ГБ" if f == int(f) else f"{f:.1f} ГБ"
    return f"{f:.0f} МБ"


def _as_disk_gb(n: object) -> str:
    if n is None:
        return "—"
    if isinstance(n, (int, float)) and n >= 0.0:
        f = float(n)
        if f == int(f):
            return f"{int(f)} ГБ"
        return f"{f} ГБ"
    return str(n)


def _coerce_int(*candidates: object) -> int | None:
    for c in candidates:
        if c is None:
            continue
        if isinstance(c, int):
            return c
        if isinstance(c, bool):
            continue
        if isinstance(c, float) and c == int(c) and 0.0 < c < 1e6:
            return int(c)
        if isinstance(c, str) and c.lstrip("+-").isdecimal():
            return int(c)
    return None
