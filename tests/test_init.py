"""Setup, entities, availability, unload and diagnostics."""

from __future__ import annotations

from datetime import timedelta
import logging
from unittest.mock import patch

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.smartboatia.api import (
    InvalidAuth,
    MqttToken,
    TemporarilyUnavailable,
)
from custom_components.smartboatia.const import CONF_API_KEY, DOMAIN

from .conftest import API_KEY, BOAT_ID, EXAMPLE_HEX, make_message

MINT = "custom_components.smartboatia.async_mint_token"
CONN = "custom_components.smartboatia.coordinator.SmartboatiaConnection"


class FakeConnection:
    """Replaces the broker connection."""

    last: FakeConnection | None = None

    def __init__(self, hass, session, api_key, *, on_payload, on_state, on_auth_failed, first_token):
        self.on_payload = on_payload
        self.started = False
        self.stopped = False
        self.api_key = api_key
        FakeConnection.last = self

    def async_start(self):
        self.started = True

    async def async_stop(self):
        self.stopped = True

    def diagnostics(self):
        return {"state": "connected", "username": "api:secret-user"}


def token():
    return MqttToken("api:secret-user", "jwt-secret", 3600.0, f"{BOAT_ID}/n", 0.0)


async def setup_entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=BOAT_ID, data={CONF_API_KEY: API_KEY})
    entry.add_to_hass(hass)
    with patch(MINT, return_value=token()), patch(CONN, FakeConnection):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_entities_from_spec_example(hass):
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert FakeConnection.last.started
    # nothing yet: no message, no data
    assert hass.states.get("sensor.smartboatia_boat_speed_over_ground") is None

    FakeConnection.last.on_payload(bytes.fromhex(EXAMPLE_HEX))
    await hass.async_block_till_done()

    sog = hass.states.get("sensor.smartboatia_boat_speed_over_ground")
    assert sog.state == "6.4"
    assert sog.attributes["unit_of_measurement"] == "kn"
    assert sog.attributes["device_class"] == "speed"
    assert hass.states.get("sensor.smartboatia_boat_water_depth").state == "8.2"
    assert hass.states.get("sensor.smartboatia_boat_board_voltage").state == "12.6"
    assert hass.states.get("sensor.smartboatia_boat_course_over_ground").state == "245.0"
    assert hass.states.get("sensor.smartboatia_boat_wind_direction_ground").state == "240.0"
    assert hass.states.get("sensor.smartboatia_boat_boat_state").state == "sailing"
    tracker = hass.states.get("device_tracker.smartboatia_boat_position")
    assert tracker.attributes["latitude"] == 53.550123
    assert tracker.attributes["longitude"] == 9.993456
    assert tracker.attributes["source_type"] == "gps"

    # one device per boat
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 1
    assert devices[0].identifiers == {(DOMAIN, BOAT_ID)}


async def test_missing_value_becomes_unavailable_and_instances_are_dynamic(hass):
    await setup_entry(hass)
    conn = FakeConnection.last
    conn.on_payload(
        make_message(
            (18, 5.0),
            (22, {0: {0: 12.6}, 1: {3: 0}}),
            (24, [{0: 41, 1: {3: 0}}, {0: 20, 1: {3: 1}}]),
        )
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.smartboatia_boat_battery_0_voltage").state == "12.6"
    assert hass.states.get("sensor.smartboatia_boat_fuel_tank_0").state == "41.0"
    assert hass.states.get("sensor.smartboatia_boat_fuel_tank_1").state == "20.0"
    assert hass.states.get("sensor.smartboatia_boat_battery_1_voltage") is None

    # next message: tank 1 and the battery are gone, a second battery shows up
    conn.on_payload(make_message((22, {0: {0: 13.0}, 1: {3: 1}}), (24, {0: 40, 1: {3: 0}})))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.smartboatia_boat_fuel_tank_1").state == "unavailable"
    assert hass.states.get("sensor.smartboatia_boat_battery_0_voltage").state == "unavailable"
    assert hass.states.get("sensor.smartboatia_boat_speed_over_ground").state == "unavailable"
    assert hass.states.get("sensor.smartboatia_boat_battery_1_voltage").state == "13.0"
    assert hass.states.get("sensor.smartboatia_boat_fuel_tank_0").state == "40.0"


async def test_garbage_is_ignored(hass, caplog):
    await setup_entry(hass)
    conn = FakeConnection.last
    with caplog.at_level(logging.DEBUG):
        conn.on_payload(b"1761578411000A53.5,9.9")
        conn.on_payload(b"\xa1\x00")
        conn.on_payload(b"")
    await hass.async_block_till_done()
    assert hass.states.get("device_tracker.smartboatia_boat_position").state == "unavailable"


async def test_stale_values_become_unavailable(hass):
    await setup_entry(hass)
    FakeConnection.last.on_payload(make_message((18, 5.0)))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.smartboatia_boat_speed_over_ground").state == "5.0"
    with patch("custom_components.smartboatia.coordinator.time.monotonic", return_value=10**9):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
        await hass.async_block_till_done()
    assert hass.states.get("sensor.smartboatia_boat_speed_over_ground").state == "unavailable"


async def test_unload_stops_connection(hass):
    entry = await setup_entry(hass)
    conn = FakeConnection.last
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert conn.stopped
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_invalid_key_starts_reauth(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=BOAT_ID, data={CONF_API_KEY: API_KEY})
    entry.add_to_hass(hass)
    with patch(MINT, side_effect=InvalidAuth()):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())


async def test_setup_cloud_unavailable_retries(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=BOAT_ID, data={CONF_API_KEY: API_KEY})
    entry.add_to_hass(hass)
    with patch(MINT, side_effect=TemporarilyUnavailable()):
        await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_diagnostics_redact_secrets(hass, hass_client):
    entry = await setup_entry(hass)
    FakeConnection.last.on_payload(make_message((18, 5.0)))
    await hass.async_block_till_done()
    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    text = str(diag)
    assert API_KEY not in text
    assert "secret-user" not in text
    assert "jwt-secret" not in text
    assert BOAT_ID not in text
    assert diag["entry"][CONF_API_KEY] == "**REDACTED**"
    assert diag["last_message"]["values"]["sog//speed"] == 5.0


async def test_api_key_never_logged(hass, caplog):
    with caplog.at_level(logging.DEBUG):
        await setup_entry(hass)
        FakeConnection.last.on_payload(make_message((18, 5.0)))
        await hass.async_block_till_done()
    assert API_KEY not in caplog.text
    assert "jwt-secret" not in caplog.text
