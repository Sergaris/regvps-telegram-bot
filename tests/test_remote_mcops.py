"""Тесты вспомогательной логики ``remote_mcops``."""

from vps_telegram_bot.remote_mcops import _ssh_client_layer_failure


def test_ssh_client_layer_failure_detects_host_key_message() -> None:
    """Текст про host key считается сбоем SSH-клиента (допустим fallback на пароль)."""

    assert _ssh_client_layer_failure(255, "", "Host key is not trusted for host x") is True


def test_ssh_client_layer_failure_detects_exit_255_without_text() -> None:
    """Код 255 без текста всё ещё трактуется как возможный сбой транспорта."""

    assert _ssh_client_layer_failure(255, "", "") is True


def test_ssh_client_layer_failure_mcops_error_not_detected() -> None:
    """Обычный выход mcops не должен маскироваться под SSH."""

    assert _ssh_client_layer_failure(1, "usage: mcops …", "") is False
