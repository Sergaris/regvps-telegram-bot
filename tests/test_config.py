"""Валидация `from_environ` с подменой словаря."""

from pathlib import Path

import pytest

from vps_telegram_bot.config import from_environ


def test_from_environ_minimal() -> None:
    """Минимально корректный набор → `AppSettings` с id и allowlist."""
    s = from_environ(
        {
            "REGRU_CLOUDVPS_TOKEN": "a",
            "REGRU_REGLET_ID": "1",
            "TELEGRAM_BOT_TOKEN": "b",
            "TELEGRAM_ALLOWED_USER_IDS": "100, 200",
        }
    )
    assert s.regru_token == "a"
    assert s.reglet_id == 1
    assert s.telegram_bot_token == "b"
    assert s.allowed_telegram_user_ids == frozenset({100, 200})
    assert "v1" in s.regru_api_base or s.regru_api_base.endswith("v1")
    assert s.mcops_remote is None


def test_from_environ_missing_allowlist_raises() -> None:
    """Пустой allowlist → `ValueError`."""
    with pytest.raises(ValueError, match="TELEGRAM_ALLOWED_USER_IDS"):
        from_environ(
            {
                "REGRU_CLOUDVPS_TOKEN": "a",
                "REGRU_REGLET_ID": "1",
                "TELEGRAM_BOT_TOKEN": "b",
                "TELEGRAM_ALLOWED_USER_IDS": "",
            }
        )


def test_from_environ_mcops_ssh_partial_raises() -> None:
    """Если задан только host без user/identity — ошибка конфигурации."""

    with pytest.raises(ValueError, match="MCOPS_SSH_USER"):
        from_environ(
            {
                "REGRU_CLOUDVPS_TOKEN": "a",
                "REGRU_REGLET_ID": "1",
                "TELEGRAM_BOT_TOKEN": "b",
                "TELEGRAM_ALLOWED_USER_IDS": "1",
                "MCOPS_SSH_HOST": "example.org",
            }
        )


def test_from_environ_mcops_ssh_ok(tmp_path: Path) -> None:
    """Полный набор MCOPS_SSH_* с существующим ключом."""

    key = tmp_path / "id_ed25519"
    key.write_text("not-a-real-key", encoding="utf-8")
    s = from_environ(
        {
            "REGRU_CLOUDVPS_TOKEN": "a",
            "REGRU_REGLET_ID": "1",
            "TELEGRAM_BOT_TOKEN": "b",
            "TELEGRAM_ALLOWED_USER_IDS": "1",
            "MCOPS_SSH_HOST": "example.org",
            "MCOPS_SSH_USER": "ops",
            "MCOPS_SSH_IDENTITY_FILE": str(key),
        }
    )
    assert s.mcops_remote is not None
    assert s.mcops_remote.host == "example.org"
    assert s.mcops_remote.user == "ops"
    assert s.mcops_remote.timeout_sec == 60.0
    assert s.mcops_remote.command_timeout_sec == 3600.0


def test_from_environ_mcops_identity_expands_env_var(tmp_path: Path, monkeypatch) -> None:
    """Windows-style env vars in SSH key path are expanded."""

    key = tmp_path / "minecraft_ops"
    key.write_text("not-a-real-key", encoding="utf-8")
    monkeypatch.setenv("MCOPS_KEY_DIR_FOR_TEST", str(tmp_path))
    s = from_environ(
        {
            "REGRU_CLOUDVPS_TOKEN": "a",
            "REGRU_REGLET_ID": "1",
            "TELEGRAM_BOT_TOKEN": "b",
            "TELEGRAM_ALLOWED_USER_IDS": "1",
            "MCOPS_SSH_HOST": "example.org",
            "MCOPS_SSH_USER": "ops",
            "MCOPS_SSH_IDENTITY_FILE": "%MCOPS_KEY_DIR_FOR_TEST%\\minecraft_ops",
            "MCOPS_SSH_TIMEOUT_SEC": "12.5",
            "MCOPS_SSH_COMMAND_TIMEOUT_SEC": "7200",
        }
    )
    assert s.mcops_remote is not None
    assert s.mcops_remote.identity_file == str(key)
    assert s.mcops_remote.timeout_sec == 12.5
    assert s.mcops_remote.command_timeout_sec == 7200.0
