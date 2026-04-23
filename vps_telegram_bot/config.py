"""Загрузка настроек из окружения."""

import os
from dataclasses import dataclass

_DEFAULT_API_BASE = "https://api.cloudvps.reg.ru/v1"
_ENV_KEY_API_BASE = "REGRU_CLOUDVPS_API_BASE"
_ENV_KEY_TOKEN = "REGRU_CLOUDVPS_TOKEN"
_ENV_KEY_REGLET = "REGRU_REGLET_ID"
_ENV_KEY_TG = "TELEGRAM_BOT_TOKEN"
_ENV_KEY_USER_IDS = "TELEGRAM_ALLOWED_USER_IDS"


@dataclass(slots=True, frozen=True, kw_only=True)
class AppSettings:
    """Конфигурация бота (секреты — только из переменных окружения)."""

    regru_api_base: str
    regru_token: str
    reglet_id: int
    telegram_bot_token: str
    allowed_telegram_user_ids: frozenset[int]
    request_timeout_sec: float


@dataclass(slots=True, frozen=True, kw_only=True)
class RawEnv:
    """Сырые строки из `os.environ` (до валидации, для тестов)."""

    regru_api_base: str
    regru_token: str
    reglet_id_raw: str
    telegram_bot_token: str
    allowed_telegram_user_ids_raw: str
    request_timeout_raw: str | None


def read_raw_environ(overrides: dict[str, str] | None = None) -> RawEnv:
    """Прочитать переменные среды или подмену для тестов.

    Args:
        overrides: При передаче используется эта map вместо `os.environ` для
            совпадения ключей.

    Returns:
        `RawEnv` с нераспарсенным id reglet и allowlist.
    """
    g = (overrides or {**os.environ}).get
    return RawEnv(
        regru_api_base=(g(_ENV_KEY_API_BASE) or _DEFAULT_API_BASE) or _DEFAULT_API_BASE,
        regru_token=(g(_ENV_KEY_TOKEN) or "").strip(),
        reglet_id_raw=(g(_ENV_KEY_REGLET) or "").strip(),
        telegram_bot_token=(g(_ENV_KEY_TG) or "").strip(),
        allowed_telegram_user_ids_raw=(g(_ENV_KEY_USER_IDS) or "").strip(),
        request_timeout_raw=g("HTTP_REQUEST_TIMEOUT_SEC", None),
    )


def _parse_allowlist_csv(raw: str) -> frozenset[int]:
    if not raw.strip():
        return frozenset()
    out: set[int] = set()
    for part in raw.split(","):
        piece = part.strip()
        if not piece:
            continue
        if not piece.isdecimal():
            msg = (
                f"{_ENV_KEY_USER_IDS} must be comma-separated numeric Telegram user ids, "
                f"got non-numeric segment: {piece!r}"
            )
            raise ValueError(msg)
        out.add(int(piece))
    return frozenset(out)


def from_environ(overrides: dict[str, str] | None = None) -> AppSettings:
    """Собрать `AppSettings` с валидацией.

    Args:
        overrides: См. `read_raw_environ`.

    Returns:
        Провалидированные настройки.

    Raises:
        ValueError: Пропущены или неверные переменные.
    """
    r = read_raw_environ(overrides)
    if not r.regru_token:
        msg = f"Missing or empty environment variable: {_ENV_KEY_TOKEN}"
        raise ValueError(msg)
    if not r.reglet_id_raw or not r.reglet_id_raw.isdecimal():
        msg = f"Missing or invalid environment variable: {_ENV_KEY_REGLET} (expected int > 0)"
        raise ValueError(msg)
    reglet_id = int(r.reglet_id_raw)
    if reglet_id <= 0:
        msg = f"{_ENV_KEY_REGLET} must be a positive int"
        raise ValueError(msg)
    if not r.telegram_bot_token:
        msg = f"Missing or empty environment variable: {_ENV_KEY_TG}"
        raise ValueError(msg)
    allowed = _parse_allowlist_csv(r.allowed_telegram_user_ids_raw)
    if not allowed:
        msg = f"{_ENV_KEY_USER_IDS} must list at least one allowed Telegram user id"
        raise ValueError(msg)
    if r.request_timeout_raw is not None and r.request_timeout_raw.strip() != "":
        request_timeout = float(r.request_timeout_raw)
        if request_timeout <= 0:
            msg = "HTTP_REQUEST_TIMEOUT_SEC must be a positive number"
            raise ValueError(msg)
    else:
        request_timeout = 30.0
    base = r.regru_api_base.rstrip("/")
    if not base:
        base = _DEFAULT_API_BASE
    return AppSettings(
        regru_api_base=base,
        regru_token=r.regru_token,
        reglet_id=reglet_id,
        telegram_bot_token=r.telegram_bot_token,
        allowed_telegram_user_ids=allowed,
        request_timeout_sec=request_timeout,
    )
