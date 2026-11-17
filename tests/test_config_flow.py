"""Config flow tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry  # noqa: F401
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smartboatia.api import (
    InvalidAuth,
    MqttToken,
    NoSubscription,
    NotBoatOwner,
    RateLimited,
    TemporarilyUnavailable,
)
from custom_components.smartboatia.const import CONF_API_KEY, DOMAIN

from .conftest import API_KEY, BOAT_ID

MINT = "custom_components.smartboatia.config_flow.async_mint_token"


def token(boat=BOAT_ID):
    return MqttToken("u", "p", 3600.0, f"{boat}/n", 0.0)


async def test_user_flow_creates_entry(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    with patch(MINT, return_value=token()), patch(
        "custom_components.smartboatia.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: f" {API_KEY} "}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_API_KEY: API_KEY}
    assert result["result"].unique_id == BOAT_ID
    assert result["title"] == "Smartboatia a1b2c3"


@pytest.mark.parametrize(
    ("exc", "error"),
    [
        (InvalidAuth(), "invalid_auth"),
        (NotBoatOwner(), "not_owner"),
        (NoSubscription(), "no_subscription"),
        (RateLimited(), "rate_limited"),
        (TemporarilyUnavailable(), "cannot_connect"),
    ],
)
async def test_user_flow_errors(hass, exc, error):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(MINT, side_effect=exc):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: API_KEY}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


async def test_bad_format_makes_no_request(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(MINT) as mint:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "not-a-key"}
        )
    mint.assert_not_called()
    assert result["errors"] == {"base": "invalid_key_format"}


async def test_already_configured(hass):
    MockConfigEntry(domain=DOMAIN, unique_id=BOAT_ID, data={CONF_API_KEY: API_KEY}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(MINT, return_value=token()):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: API_KEY}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def _start_reauth(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=BOAT_ID, data={CONF_API_KEY: API_KEY})
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    return entry, result


async def test_reauth_success(hass):
    entry, result = await _start_reauth(hass)
    new_key = "sbk_" + "B" * 43
    with patch(MINT, return_value=token()), patch(
        "custom_components.smartboatia.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: new_key}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == new_key


async def test_reauth_other_boat(hass):
    entry, result = await _start_reauth(hass)
    with patch(MINT, return_value=token("other-boat")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "sbk_" + "B" * 43}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_boat"
    assert entry.data[CONF_API_KEY] == API_KEY


async def test_reauth_invalid_key_stays_on_form(hass):
    _entry, result = await _start_reauth(hass)
    with patch(MINT, side_effect=InvalidAuth()):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "sbk_" + "B" * 43}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
