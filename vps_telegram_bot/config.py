"""Загрузка настроек из окружения."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_API_BASE = "https://api.cloudvps.reg.ru/v1"
_ENV_KEY_API_BASE = "REGRU_CLOUDVPS_API_BASE"
_ENV_KEY_TOKEN = "REGRU_CLOUDVPS_TOKEN"
_ENV_KEY_REGLET = "REGRU_REGLET_ID"
_ENV_KEY_TG = "TELEGRAM_BOT_TOKEN"
_ENV_KEY_USER_IDS = "TELEGRAM_ALLOWED_USER_IDS"
_ENV_KEY_TG_CONNECT = "TELEGRAM_HTTP_CONNECT_TIMEOUT_SEC"
_ENV_KEY_TG_READ = "TELEGRAM_HTTP_READ_TIMEOUT_SEC"
_ENV_KEY_TG_WRITE = "TELEGRAM_HTTP_WRITE_TIMEOUT_SEC"
_ENV_KEY_TG_POOL = "TELEGRAM_HTTP_POOL_TIMEOUT_SEC"


@dataclass(slots=True, frozen=True, kw_only=True)
class McopsRemoteSettings:
    """SSH-доступ к хосту Minecraft для вызова ``mcops`` CLI.

    Можно задать только ключ, только пароль или оба: при двух значениях
    ``run_remote_mcops`` сначала пробует ключ, затем пароль при сбое SSH.
    """

    host: str
    user: str
    identity_file: str | None
    ssh_password: str | None
    port: int
    remote_cwd: str
    remote_python: str
    timeout_sec: float
    command_timeout_sec: float


@dataclass(slots=True, frozen=True, kw_only=True)
class AppSettings:
    """Конфигурация бота (секреты — только из переменных окружения)."""

    regru_api_base: str
    regru_token: str
    reglet_id: int
    telegram_bot_token: str
    allowed_telegram_user_ids: frozenset[int]
    request_timeout_sec: float
    telegram_http_connect_timeout_sec: float
    telegram_http_read_timeout_sec: float
    telegram_http_write_timeout_sec: float
    telegram_http_pool_timeout_sec: float
    mcops_remote: McopsRemoteSettings | None


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


def _parse_positive_float(
    env: dict[str, str],
    key: str,
    *,
    default: float,
) -> float:
    """Разобрать положительный float из окружения или вернуть default.

    Args:
        env: Карта переменных (подмена для тестов или `os.environ`).
        key: Имя переменной.
        default: Значение, если переменная не задана или пустая.

    Returns:
        Положительное число секунд.

    Raises:
        ValueError: Не число или значение <= 0.
    """
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    value = float(raw)
    if value <= 0:
        msg = f"{key} must be a positive number"
        raise ValueError(msg)
    return value


def _expand_windows_style_env_vars(text: str) -> str:
    """Подставить ``%VAR%`` из окружения (в т.ч. на Linux, где нет ``cmd``-синтаксиса)."""

    def _replace(match: re.Match[str]) -> str:
        return os.environ.get(match.group(1), match.group(0))

    return re.sub(r"%([^%]+)%", _replace, text)


def _parse_mcops_remote(env: dict[str, str]) -> McopsRemoteSettings | None:
    """Разобрать SSH-настройки для ``mcops`` или вернуть ``None``."""

    host = (env.get("MCOPS_SSH_HOST") or "").strip()
    if not host:
        return None
    user = (env.get("MCOPS_SSH_USER") or "").strip()
    identity = (env.get("MCOPS_SSH_IDENTITY_FILE") or "").strip()
    password = (env.get("MCOPS_SSH_PASSWORD") or "").strip()
    if not user:
        msg = "MCOPS_SSH_HOST set but MCOPS_SSH_USER is empty"
        raise ValueError(msg)
    if not identity and not password:
        msg = (
            "MCOPS_SSH_HOST set but neither MCOPS_SSH_IDENTITY_FILE nor MCOPS_SSH_PASSWORD is set "
            "(можно задать оба: сначала ключ, при ошибке SSH — пароль)"
        )
        raise ValueError(msg)
    path: Path | None = None
    if identity:
        expanded = _expand_windows_style_env_vars(identity).replace("\\", "/")
        path = Path(os.path.expandvars(expanded)).expanduser()
        if not path.is_file():
            msg = f"MCOPS_SSH_IDENTITY_FILE is not a file: {path}"
            raise ValueError(msg)
    port_raw = (env.get("MCOPS_SSH_PORT") or "22").strip()
    if not port_raw.isdecimal():
        msg = "MCOPS_SSH_PORT must be a positive integer"
        raise ValueError(msg)
    port = int(port_raw)
    if port <= 0:
        msg = "MCOPS_SSH_PORT must be a positive integer"
        raise ValueError(msg)
    cwd = (env.get("MCOPS_SSH_REMOTE_CWD") or "/opt/minecraft/ops").strip()
    py = (env.get("MCOPS_SSH_REMOTE_PYTHON") or "python3").strip()
    timeout_raw = (env.get("MCOPS_SSH_TIMEOUT_SEC") or "60").strip()
    timeout = float(timeout_raw)
    if timeout <= 0:
        msg = "MCOPS_SSH_TIMEOUT_SEC must be a positive number"
        raise ValueError(msg)
    command_timeout_raw = (env.get("MCOPS_SSH_COMMAND_TIMEOUT_SEC") or "3600").strip()
    command_timeout = float(command_timeout_raw)
    if command_timeout <= 0:
        msg = "MCOPS_SSH_COMMAND_TIMEOUT_SEC must be a positive number"
        raise ValueError(msg)
    return McopsRemoteSettings(
        host=host,
        user=user,
        identity_file=str(path) if path is not None else None,
        ssh_password=password if password else None,
        port=port,
        remote_cwd=cwd,
        remote_python=py,
        timeout_sec=timeout,
        command_timeout_sec=command_timeout,
    )


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
    env_map = dict(overrides or {**os.environ})
    mcops_remote = _parse_mcops_remote(env_map)
    tg_connect = _parse_positive_float(
        env_map,
        _ENV_KEY_TG_CONNECT,
        default=30.0,
    )
    tg_read = _parse_positive_float(
        env_map,
        _ENV_KEY_TG_READ,
        default=60.0,
    )
    tg_write = _parse_positive_float(
        env_map,
        _ENV_KEY_TG_WRITE,
        default=30.0,
    )
    tg_pool = _parse_positive_float(
        env_map,
        _ENV_KEY_TG_POOL,
        default=20.0,
    )
    return AppSettings(
        regru_api_base=base,
        regru_token=r.regru_token,
        reglet_id=reglet_id,
        telegram_bot_token=r.telegram_bot_token,
        allowed_telegram_user_ids=allowed,
        request_timeout_sec=request_timeout,
        telegram_http_connect_timeout_sec=tg_connect,
        telegram_http_read_timeout_sec=tg_read,
        telegram_http_write_timeout_sec=tg_write,
        telegram_http_pool_timeout_sec=tg_pool,
        mcops_remote=mcops_remote,
    )
