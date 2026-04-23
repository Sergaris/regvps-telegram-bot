"""HTTP-клиент к Reg.ru CloudVPS: действия с reglet."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx

log = logging.getLogger(__name__)


class RegletAction(StrEnum):
    """Тип `POST` к `/reglets/{id}/actions` (поле `type`)."""

    START = "start"
    STOP = "stop"
    REBOOT = "reboot"


class RegRuClientError(Exception):
    """Сетевая, транспортная или HTTP-ошибка (без 2xx)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message: str = message
        self.status_code: int | None = None
        self.response_text: str | None = None


@dataclass(slots=True, kw_only=True)
class RegRuClient:
    """Async-клиент: `httpx.AsyncClient` с Bearer, `base_url` = API root (с `/v1`).

    Attributes:
        regru_api_base: Напр. `https://api.cloudvps.reg.ru/v1` (без завершающего `/`)
        _client: Внутренний клиент; не переиспользовайте вне `RegRuClient` после `aclose`.
    """

    regru_api_base: str
    token: str
    reglet_id: int
    request_timeout_sec: float = 30.0
    _client: httpx.AsyncClient | None = field(default=None, repr=False)

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=(self.regru_api_base.rstrip("/") + "/"),
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                timeout=self.request_timeout_sec,
            )
        return self._client

    async def aclose(self) -> None:
        """Закрыть `httpx.AsyncClient`. Idempotent."""
        c = self._client
        self._client = None
        if c is not None:
            await c.aclose()

    async def _get_json(self, rel: str) -> dict:
        """`GET` относительный `rel`, ответ 200, тело `dict` в JSON.

        Args:
            rel: Путь, например `reglets` (к `base_url` панели).

        Returns:
            Словарь.

        Raises:
            `RegRuClientError`: транспорт, 4xx/5xx, не-JSON, не-`dict` в корне.
        """
        client = self._ensure_client()
        try:
            response = await client.get(rel)
        except httpx.HTTPError as e:
            log.error("Reg.ru GET %s failed: network: %s", rel, e)
            raise RegRuClientError("Сеть: не удалось связаться с панелью. Повторите позже.") from e
        if response.status_code == 200:
            try:
                data = response.json()
            except ValueError as e:
                log.error("Reg.ru GET %s: not JSON: %s", rel, e)
                raise RegRuClientError("Панель вернула не-JSON. Повторите позже.") from e
            if not isinstance(data, dict):
                log.error("Reg.ru GET %s: root is not a dict: %r", rel, type(data))
                msg = "Панель вернула неожиданный JSON (ожидался объект). Повторите позже."
                raise RegRuClientError(msg)
            return data
        text_head = (response.text or "")[:500]
        err = RegRuClientError("Панель отклонила запрос. Попробуйте позже.")
        err.status_code = response.status_code
        err.response_text = text_head
        log.error("Reg.ru GET %s failed: HTTP %s, body: %s", rel, response.status_code, text_head)
        raise err

    async def fetch_reglets(self) -> dict:
        """`GET /reglets` — полный JSON (корневой dict с `reglets`, `links` и т.д.).

        Returns:
            Словарь, распарсенный из JSON-ответа.

        Raises:
            `RegRuClientError`: сеть, не-200, невалидный JSON, корень не `dict`.
        """
        return await self._get_json("reglets")

    async def fetch_reglet(self) -> dict:
        """`GET /reglets/{id}` — детализация одного сервера (там `disk_usage` в ГБ по доке).

        В ответе `GET /v1/reglets` (список) `disk_usage` в объектах часто `0.0` или
        вовсе нет; актуальное **занято** на диске Reg.ru вводит в поле
        [«фактический размер диска, ГБ»](https://developers.cloudvps.reg.ru/reglets/info.html).

        Returns:
            Корневой JSON, обычно ключ `reglet` с вложенным объектом.
        """
        return await self._get_json(f"reglets/{self.reglet_id}")

    async def fetch_balance_data(self) -> dict:
        """`GET /balance_data` — баланс аккаунта (корневой JSON, как `curl` + `jq`).

        Returns:
            Словарь, обычно вложенный `balance_data.balance` (руб.).

        Raises:
            `RegRuClientError`: сеть, не-200, JSON.
        """
        return await self._get_json("balance_data")

    async def post_reglet_action(self, action: RegletAction) -> str:
        """`POST` action для reglet. Возвращает краткую фразу для чата (рус.).

        Args:
            action: `start` / `stop` / `reboot`.

        Returns:
            Текст пользователю.

        Raises:
            `RegRuClientError`: сеть, не 2xx, неожиданный код.
        """
        rel = f"reglets/{self.reglet_id}/actions"
        payload: dict[str, str] = {"type": str(action)}

        client = self._ensure_client()
        try:
            response = await client.post(rel, json=payload)
        except httpx.HTTPError as e:
            log.error("Reg.ru request failed: network or transport: %s", e)
            raise RegRuClientError("Сеть: не удалось связаться с панелью. Повторите позже.") from e
        if response.status_code in (200, 201, 202, 204):
            log.info("Reg.ru reglet action %s: HTTP %s", action, response.status_code)
            return _action_success_message(action)
        if response.is_server_error or response.is_client_error:
            text_head = (response.text or "")[:200]
            err = RegRuClientError("Панель отклонила запрос. Попробуйте позже.")
            err.status_code = response.status_code
            err.response_text = text_head
            log.error(
                "Reg.ru reglet action %s failed: HTTP %s, body: %s",
                action,
                response.status_code,
                text_head,
            )
            raise err
        err = RegRuClientError("Панель вернула неожиданный ответ.")
        err.status_code = response.status_code
        err.response_text = (response.text or "")[:200]
        log.error(
            "Reg.ru reglet action %s unexpected: HTTP %s, body: %s",
            action,
            response.status_code,
            err.response_text,
        )
        raise err


def _action_success_message(action: RegletAction) -> str:
    match action:
        case RegletAction.START:
            return "Запрос на запуск VPS отправлен (start)."
        case RegletAction.STOP:
            return "Запрос на остановку VPS отправлен (stop)."
        case RegletAction.REBOOT:
            return "Запрос на перезагрузку VPS отправлен (reboot)."
        case _:
            raise NotImplementedError(action)


def format_balance_telegram(api_root: Mapping[str, Any]) -> str:
    """Строка для чата по `GET /v1/balance_data` → `jq .balance_data.balance` (в рублях).

    Args:
        api_root: Корневой JSON, как в ответе панели.

    Returns:
        Краткое русское сообщение, без внутренних путей API при ошибке разбора.
    """
    bd = api_root.get("balance_data")
    if not isinstance(bd, dict):
        return "Панель не вернула `balance_data` (объект) — смотрите сырой ответ API в панели."
    bal = bd.get("balance")
    if bal is None:
        return "В `balance_data` нет поля `balance`."
    f_val: float
    if isinstance(bal, (int, float)) and not isinstance(bal, bool):
        f_val = float(bal)
    else:
        t = str(bal).strip().replace(" ", "").replace(",", ".")
        try:
            f_val = float(t)
        except ValueError:
            return f"Баланс: не разобрать как число ({bal!s})."
    s = f"{f_val:,.2f} ₽".replace(",", " ")
    return f"Баланс: {s}"
