"""Точка входа: `python -m vps_telegram_bot` или `vps-telegram`."""

import logging
from logging import config as logging_config

from telegram import Update

from vps_telegram_bot.bot import build_application
from vps_telegram_bot.config import from_environ
from vps_telegram_bot.dotenv_bootstrap import load_env_file
from vps_telegram_bot.regru_client import RegRuClient


def _setup_logging() -> None:
    """Настроить `logging` на stdout (для Docker/systemd)."""
    logging_config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "formatter": "default",
                },
            },
            "root": {"level": "INFO", "handlers": ["console"]},
        }
    )


def main() -> None:
    """Считать env, собрать приложение, long polling до отмены (Ctrl+C)."""
    _setup_logging()
    log = logging.getLogger("vps_telegram_bot")
    if load_env_file(override=False):
        log.info("Loaded environment from project root .env (does not override existing OS env).")
    try:
        settings = from_environ()
    except ValueError as e:
        log.error("Config error: %s", e)
        raise SystemExit(1) from e
    regru = RegRuClient(
        regru_api_base=settings.regru_api_base,
        token=settings.regru_token,
        reglet_id=settings.reglet_id,
        request_timeout_sec=settings.request_timeout_sec,
    )
    app = build_application(settings, regru)
    log.info("Bot starting (long polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
