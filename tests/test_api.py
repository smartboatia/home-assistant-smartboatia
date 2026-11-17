"""Token exchange tests."""

from __future__ import annotations

import pytest

from custom_components.smartboatia.api import (
    InvalidAuth,
    MqttToken,
    NoSubscription,
    NotBoatOwner,
    RateLimited,
    TemporarilyUnavailable,
    async_mint_token,
    parse_token_response,
)
from custom_components.smartboatia.const import API_BASE_URL, TOKEN_PATH

from .conftest import API_KEY

URL = API_BASE_URL + TOKEN_PATH

BODY = {
    "v": 1,
    "t": 1761578411000,
    "status": 200,
    "status_description": "OK",
    "credentials": {"username": "api:1234", "password": "jwt-secret"},
    "expires_at": 1761582011,
    "expires_in": 3600,
    "topic": "boat-1/n",
}


def test_parse_envelope():
    token = parse_token_response(BODY, 100.0)
    assert token.topic == "boat-1/n"
    assert token.boat_id == "boat-1"
    assert token.expires_at_monotonic == 3700.0


def test_repr_hides_secrets():
    token = parse_token_response(BODY, 0.0)
    for text in (repr(token), str(token), f"{token}"):
        assert "jwt-secret" not in text
        assert "api:1234" not in text


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {**BODY, "credentials": {}},
        {**BODY, "expires_in": 0},
        {**BODY, "expires_in": 5},
        {**BODY, "expires_in": True},
        {**BODY, "expires_in": "3600"},
        {**BODY, "topic": ""},
        {**BODY, "credentials": {"username": "", "password": "x"}},
    ],
)
def test_parse_malformed(body):
    with pytest.raises(TemporarilyUnavailable):
        parse_token_response(body, 0.0)


async def test_mint_ok(hass, aioclient_mock):
    aioclient_mock.post(URL, json=BODY)
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    token = await async_mint_token(async_get_clientsession(hass), API_KEY)
    assert isinstance(token, MqttToken)
    call = aioclient_mock.mock_calls[0]
    assert call[0] == "POST"
    assert call[3]["X-API-Key"] == API_KEY
    assert call[2] is None  # no body


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, InvalidAuth),
        (403, NotBoatOwner),
        (402, NoSubscription),
        (429, RateLimited),
        (503, TemporarilyUnavailable),
        (500, TemporarilyUnavailable),
    ],
)
async def test_mint_errors(hass, aioclient_mock, status, error):
    aioclient_mock.post(URL, status=status)
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    with pytest.raises(error):
        await async_mint_token(async_get_clientsession(hass), API_KEY)


async def test_retry_after(hass, aioclient_mock):
    aioclient_mock.post(URL, status=429, headers={"Retry-After": "120"})
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    with pytest.raises(RateLimited) as err:
        await async_mint_token(async_get_clientsession(hass), API_KEY)
    assert err.value.retry_after == 120


async def test_network_error(hass, aioclient_mock):
    import aiohttp

    aioclient_mock.post(URL, exc=aiohttp.ClientError("boom"))
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    with pytest.raises(TemporarilyUnavailable):
        await async_mint_token(async_get_clientsession(hass), API_KEY)


@pytest.mark.parametrize("status", [200, 401, 500])
async def test_key_and_token_never_logged(hass, aioclient_mock, caplog, status):
    import logging

    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    aioclient_mock.post(URL, status=status, json=BODY if status == 200 else {})
    with caplog.at_level(logging.DEBUG):
        try:
            await async_mint_token(async_get_clientsession(hass), API_KEY)
        except Exception:  # noqa: BLE001
            pass
    assert API_KEY not in caplog.text
    assert "jwt-secret" not in caplog.text
    assert "api:1234" not in caplog.text
