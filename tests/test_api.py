"""Tests for the PDN Express API client."""

import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.pdn_express.api import PDNExpressApiClient, PDNExpressApiError

from .payloads import delivered_sample, load_fixture

CODE = "PDN0000000001"


def _session_returning(status: int, body: object = None, headers=None) -> MagicMock:
    response = AsyncMock()
    response.status = status
    response.headers = headers or {}
    if isinstance(body, str):
        response.json = AsyncMock(side_effect=json.JSONDecodeError("x", body, 0))
    else:
        response.json = AsyncMock(return_value=body)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=ctx)
    return session


def _ok(entry: dict) -> dict:
    return {"ok": True, "data": [entry]}


async def test_get_parcel_posts_one_number_and_returns_the_entry():
    session = _session_returning(200, _ok(delivered_sample(CODE)))
    parcel = await PDNExpressApiClient(session).async_get_parcel(CODE)

    assert parcel["orderNo"] == CODE
    assert session.post.call_args.kwargs["json"] == {
        "action": "search",
        "orderNos": CODE,
        "locale": "en",
    }


async def test_get_parcel_returns_none_for_the_not_found_fixture():
    body = load_fixture("fixture_not_found")
    code = body["data"][0]["orderNo"]
    client = PDNExpressApiClient(_session_returning(200, body))
    assert await client.async_get_parcel(code) is None


async def test_inconclusive_eventless_entry_warns_once_and_keeps_last_good(caplog):
    body = _ok({"orderNo": CODE, "statusCodeConfig": {"lanName": "Maintenance"}})
    client = PDNExpressApiClient(_session_returning(200, body))
    assert await client.async_get_parcel(CODE) is None
    assert await client.async_get_parcel(CODE) is None
    assert caplog.text.count("unrecognised notice") == 1


async def test_eventless_entry_without_config_is_inconclusive(caplog):
    client = PDNExpressApiClient(_session_returning(200, _ok({"orderNo": CODE})))
    assert await client.async_get_parcel(CODE) is None
    assert "unrecognised notice" in caplog.text


async def test_order_mismatch_is_ignored_with_a_warning(caplog):
    client = PDNExpressApiClient(
        _session_returning(200, _ok(delivered_sample("PDN0000000099")))
    )
    assert await client.async_get_parcel(CODE) is None
    assert "different number" in caplog.text


async def test_upstream_failure_is_a_retryable_error_without_warning(caplog):
    client = PDNExpressApiClient(
        _session_returning(200, {"ok": False, "error": "upstream"})
    )
    with pytest.raises(PDNExpressApiError) as err:
        await client.async_get_parcel(CODE)
    assert err.value.detail == "upstream"
    assert "unrecognised failure" not in caplog.text


async def test_new_error_string_warns_once(caplog):
    client = PDNExpressApiClient(
        _session_returning(200, {"ok": False, "error": "invalid_json"})
    )
    for _ in range(2):
        with pytest.raises(PDNExpressApiError):
            await client.async_get_parcel(CODE)
    assert caplog.text.count("invalid_json") == 1


async def test_failure_envelope_without_detail_raises():
    client = PDNExpressApiClient(_session_returning(200, {"ok": False}))
    with pytest.raises(PDNExpressApiError):
        await client.async_get_parcel(CODE)


@pytest.mark.parametrize(
    "body", [{"ok": True, "data": []}, {"ok": True, "data": ["x"]}, {"ok": True}]
)
async def test_missing_entry_raises(body):
    client = PDNExpressApiClient(_session_returning(200, body))
    with pytest.raises(PDNExpressApiError):
        await client.async_get_parcel(CODE)


@pytest.mark.parametrize("status", [403, 404, 500])
async def test_error_status_raises_with_code(status):
    client = PDNExpressApiClient(_session_returning(status, {}))
    with pytest.raises(PDNExpressApiError) as err:
        await client.async_get_parcel(CODE)
    assert err.value.status_code == status


async def test_rate_limit_carries_retry_after():
    client = PDNExpressApiClient(_session_returning(429, {}, {"Retry-After": "120"}))
    with pytest.raises(PDNExpressApiError) as err:
        await client.async_get_parcel(CODE)
    assert err.value.status_code == 429 and err.value.retry_after == 120.0


@pytest.mark.parametrize(
    "headers", [{}, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}]
)
async def test_rate_limit_without_seconds_has_no_retry_after(headers):
    client = PDNExpressApiClient(_session_returning(429, {}, headers))
    with pytest.raises(PDNExpressApiError) as err:
        await client.async_get_parcel(CODE)
    assert err.value.retry_after is None


async def test_unparseable_body_raises():
    client = PDNExpressApiClient(_session_returning(200, "<html>"))
    with pytest.raises(PDNExpressApiError):
        await client.async_get_parcel(CODE)


async def test_non_object_body_raises():
    client = PDNExpressApiClient(_session_returning(200, ["not", "a", "dict"]))
    with pytest.raises(PDNExpressApiError):
        await client.async_get_parcel(CODE)


async def test_network_error_propagates():
    session = MagicMock()
    session.post = MagicMock(side_effect=aiohttp.ClientError("boom"))
    with pytest.raises(aiohttp.ClientError):
        await PDNExpressApiClient(session).async_get_parcel(CODE)
