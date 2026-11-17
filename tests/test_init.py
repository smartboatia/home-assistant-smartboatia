"""Setup, entities, availability, unload and diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.smartboatia.const import CONF_API_KEY, DOMAIN

from .conftest import API_KEY, BOAT_ID, EXAMPLE_HEX, EXAMPLE_TS, make_message

MINT = "custom_components.smartboatia.connection.async_mint_token"
NOW = "custom_components.smartboatia.coordinator._now"

T0 = datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC)


def ms(delta_seconds: float) -> int:
    """Message timestamp (ms) at T0 plus an offset."""
    return int((T0.timestamp() + delta_seconds) * 1000)

CONN = "custom_components.smartboatia.coordinator.SmartboatiaConnection"


class FakeConnection:
    """Replaces the broker connection."""

    last: FakeConnection | None = None

    def __init__(self, hass, session, api_key, *, on_payload, on_state, on_auth_failed):
        self.on_payload = on_payload
        self.on_auth_failed = on_auth_failed
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


async def setup_entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=BOAT_ID, data={CONF_API_KEY: API_KEY})
    entry.add_to_hass(hass)
    with patch(CONN, FakeConnection):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_entities_from_spec_example(hass):
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert FakeConnection.last.started
    # nothing yet: no message, no data
    assert hass.states.get("sensor.smartboatia_boat_speed_over_ground") is None

    with patch(NOW, return_value=datetime.fromtimestamp(EXAMPLE_TS / 1000 + 5, UTC)):
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
    assert caplog.text.count("Ignoring a message that is not N-message v3") == 2
    assert caplog.text.count("Discarding an invalid N-message") == 1
    assert "1761578411000A" not in caplog.text


async def test_stale_values_become_unavailable(hass):
    await setup_entry(hass)
    FakeConnection.last.on_payload(make_message((18, 5.0)))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.smartboatia_boat_speed_over_ground").state == "5.0"
    with patch("custom_components.smartboatia.coordinator.time.monotonic", return_value=10**9):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
        await hass.async_block_till_done()
    assert hass.states.get("sensor.smartboatia_boat_speed_over_ground").state == "unavailable"
    # the timestamp of the last message stays visible
    assert hass.states.get("sensor.smartboatia_boat_last_update").state != "unavailable"


async def test_unload_stops_connection(hass):
    entry = await setup_entry(hass)
    conn = FakeConnection.last
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert conn.stopped
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_does_not_mint_and_survives_a_dead_cloud(hass):
    """Setup returns at once; the connection owns the exchanges and their backoff."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=BOAT_ID, data={CONF_API_KEY: API_KEY})
    entry.add_to_hass(hass)
    with patch(CONN, FakeConnection), patch(MINT, new=AsyncMock()) as mint:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    mint.assert_not_called()
    assert entry.state is ConfigEntryState.LOADED


async def test_rejected_key_starts_reauth(hass):
    entry = await setup_entry(hass)
    FakeConnection.last.on_auth_failed()
    await hass.async_block_till_done()
    assert any(
        f["context"]["source"] == "reauth" and f["context"]["entry_id"] == entry.entry_id
        for f in hass.config_entries.flow.async_progress()
    )


async def test_homeassistant_stop_disconnects(hass):
    await setup_entry(hass)
    conn = FakeConnection.last
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert conn.stopped


async def test_old_retained_message_is_not_live(hass):
    """A boat offline for a day: the replayed message must not look current."""
    await setup_entry(hass)
    conn = FakeConnection.last
    with patch(NOW, return_value=T0):
        conn.on_payload(
            make_message((18, 5.0), (10, {0: 53.55, 1: 9.99}), ts=ms(-86400))
        )
        await hass.async_block_till_done()
        assert hass.states.get("sensor.smartboatia_boat_speed_over_ground") is None
        tracker = hass.states.get("device_tracker.smartboatia_boat_position")
        assert tracker.state == "unavailable"
        # the user can still see how old the data is
        last = hass.states.get("sensor.smartboatia_boat_last_update")
        assert last.state == (T0 - timedelta(days=1)).isoformat()

        # a fresh message takes over
        conn.on_payload(make_message((18, 5.0), ts=ms(-30)))
        await hass.async_block_till_done()
    assert hass.states.get("sensor.smartboatia_boat_speed_over_ground").state == "5.0"
    last = hass.states.get("sensor.smartboatia_boat_last_update")
    assert last.state == (T0 - timedelta(seconds=30)).isoformat()


@pytest.mark.parametrize(
    ("offset", "live"),
    [
        (-30, True),
        (-180, True),  # Hub clock a few minutes behind
        (+180, True),  # Hub clock a few minutes ahead
        (+7200, True),  # far ahead cannot be an old replay
        (-590, True),
        (-660, False),  # older than the stale limit
    ],
)
async def test_message_age_and_clock_skew(hass, offset, live):
    await setup_entry(hass)
    with patch(NOW, return_value=T0):
        FakeConnection.last.on_payload(make_message((18, 5.0), ts=ms(offset)))
        await hass.async_block_till_done()
    sog = hass.states.get("sensor.smartboatia_boat_speed_over_ground")
    if live:
        assert sog.state == "5.0"
    else:
        assert sog is None


async def test_diagnostics_redact_secrets(hass, hass_client):
    entry = await setup_entry(hass)
    FakeConnection.last.on_payload(
        make_message((18, 5.0), (10, {0: 53.5512, 1: 9.9876}))
    )
    await hass.async_block_till_done()
    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    text = str(diag)
    assert API_KEY not in text
    assert "secret-user" not in text
    assert "jwt-secret" not in text
    assert BOAT_ID not in text
    assert diag["entry"][CONF_API_KEY] == "**REDACTED**"
    assert diag["last_message"]["values"]["sog//speed"] == 5.0
    # the live position is redacted
    assert diag["last_message"]["values"]["gps_pos//lat"] == "**REDACTED**"
    assert diag["last_message"]["values"]["gps_pos//long"] == "**REDACTED**"
    assert "53.5512" not in text
    assert "9.9876" not in text


async def test_api_key_never_logged(hass, caplog):
    with caplog.at_level(logging.DEBUG):
        await setup_entry(hass)
        FakeConnection.last.on_payload(make_message((18, 5.0)))
        await hass.async_block_till_done()
    assert API_KEY not in caplog.text
    assert "jwt-secret" not in caplog.text
