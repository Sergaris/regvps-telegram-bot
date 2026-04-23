"""Тесты `RegRuClient` с подменой HTTP (respx)."""

import json

import httpx
import pytest
import respx

from vps_telegram_bot.regru_client import RegletAction, RegRuClient, RegRuClientError

_API = "https://api.cloudvps.reg.ru/v1"
_PATH = f"{_API}/reglets/7027955/actions"


@pytest.mark.asyncio
@respx.mock
async def test_post_reglet_action_sends_bearer_and_json() -> None:
    """Успешный ответ: POST с `type: start` и `Authorization: Bearer <token>`."""
    respx.post(_PATH).mock(return_value=httpx.Response(202, json={"id": 1}))
    c = RegRuClient(
        regru_api_base=_API,
        token="test-token",
        reglet_id=7027955,
        request_timeout_sec=5.0,
    )
    try:
        out = await c.post_reglet_action(RegletAction.START)
        assert "запуск" in out
        r = respx.calls.last.request
        assert r.method == "POST"
        assert r.headers.get("Authorization") == "Bearer test-token"
        assert json.loads(r.content) == {"type": "start"}
    finally:
        await c.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_fetch_reglets_get_200() -> None:
    """`GET` отдаёт JSON с `reglets` (проверка пути + успех)."""
    url = f"{_API}/reglets"
    respx.get(url).mock(return_value=httpx.Response(200, json={"reglets": [], "links": {}}))
    c = RegRuClient(
        regru_api_base=_API,
        token="x",
        reglet_id=1,
        request_timeout_sec=2.0,
    )
    try:
        d = await c.fetch_reglets()
        assert d == {"reglets": [], "links": {}}
    finally:
        await c.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_fetch_reglet_get_200() -> None:
    """`GET /reglets/{id}` — для `disk_usage` (ГБ) по одному серверу."""
    url = f"{_API}/reglets/99"
    respx.get(url).mock(
        return_value=httpx.Response(200, json={"reglet": {"id": 99, "disk": 10, "disk_usage": 6.7}})
    )
    c = RegRuClient(
        regru_api_base=_API,
        token="x",
        reglet_id=99,
        request_timeout_sec=2.0,
    )
    try:
        d = await c.fetch_reglet()
        assert d["reglet"]["disk_usage"] == 6.7
    finally:
        await c.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_fetch_balance_data_get_200() -> None:
    """`GET /balance_data` — как `curl` + `jq .balance_data.balance`."""
    url = f"{_API}/balance_data"
    respx.get(url).mock(
        return_value=httpx.Response(
            200,
            json={"balance_data": {"balance": 100.12, "currency": "RUB"}},
        )
    )
    c = RegRuClient(
        regru_api_base=_API,
        token="x",
        reglet_id=1,
        request_timeout_sec=2.0,
    )
    try:
        d = await c.fetch_balance_data()
        assert d["balance_data"]["balance"] == 100.12
    finally:
        await c.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_post_reglet_action_4xx_raises() -> None:
    """4xx/5xx → `RegRuClientError`."""
    respx.post(_PATH).mock(return_value=httpx.Response(401, text="nope"))
    c = RegRuClient(regru_api_base=_API, token="t", reglet_id=7027955, request_timeout_sec=2.0)
    try:
        with pytest.raises(RegRuClientError):
            await c.post_reglet_action(RegletAction.STOP)
    finally:
        await c.aclose()
