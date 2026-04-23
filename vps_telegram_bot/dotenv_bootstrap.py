"""Опциональная загрузка `KEY=value` из `.env` в `os.environ` (локальная разработка)."""

from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_env_file(override: bool = False) -> bool:
    """Прочитать корневой `.env`, если файл есть.

    Args:
        override: Если `True`, существующие в ОС переменные с теми же именами
            перезаписываются значениями из файла. Обычно `False` — в проде/CI
            приоритет у заранее заданного окружения.

    Returns:
        `True`, если `load_dotenv` что-то применил (см. доку `python-dotenv`).
    """
    from dotenv import load_dotenv

    return bool(load_dotenv(_PROJECT_ROOT / ".env", override=override))
