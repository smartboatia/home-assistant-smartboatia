"""Fixed sensors at setup, discovery notification and the Hub update repair issue."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er, issue_registry as ir

from custom_components.smartboatia.const import DOMAIN
from custom_components.smartboatia.sensor import (
    FIXED_VALUE_KEYS,
    description_for,
    discovery_text,
)

from .conftest import BOAT_ID, make_message
from .test_init import CONN, FakeConnection, setup_entry

COMPONENT = Path(__file__).parents[1] / "custom_components" / "smartboatia"
NOTIFY = "custom_components.smartboatia.sensor.persistent_notification.async_create"
LEGACY = b"1795006111952B13.03I973.8D0.00379N1W"
SOG = "sensor.smartboatia_boat_a1b2c3_speed_over_ground"

# unique id suffix (after the boat id) of every fixed sensor
FIXED_UNIQUE_IDS = {
    "voltage_voltage",
    "heading_true_heading",
    "sea_roughness_roughness",
    "wind_ground_speed",
    "wind_true_speed",
    "wind_apparent_speed",
    "wind_ground_direction",
    "wind_true_direction",
    "wind_apparent_rel_angle",
    "water_depth_depth",
    "atmospheric_pressure_pressure",
    "stw_speed",
    "sog_speed",
    "cog_true_course",
    "cog_magnetic_course",
    "water_temperature_temperature",
    "log_event_type",
}


def sensor_unique_ids(hass, entry) -> set[str]:
    registry = er.async_get(hass)
    return {
        e.unique_id
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.domain == "sensor"
    }


def issue(hass, entry):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"hub_update_needed_{entry.entry_id}"
    )


async def test_fixed_sensors_exist_at_setup_without_data(hass):
    entry = await setup_entry(hass)
    assert sensor_unique_ids(hass, entry) == {
        f"{BOAT_ID}_{s}" for s in FIXED_UNIQUE_IDS | {"last_update"}
    }
    assert len(FIXED_VALUE_KEYS) == len(FIXED_UNIQUE_IDS)
    assert hass.states.get(SOG).state == "unavailable"
    assert (
        hass.states.get("sensor.smartboatia_boat_a1b2c3_boat_state").state
        == "unavailable"
    )
    # instances are not created without data
    assert hass.states.get("sensor.smartboatia_boat_a1b2c3_battery_0_voltage") is None


async def test_message_feeds_the_precreated_entity(hass):
    entry = await setup_entry(hass)
    registry = er.async_get(hass)
    before = registry.async_get(SOG)
    FakeConnection.last.on_payload(make_message((18, 5.0)))
    await hass.async_block_till_done()
    assert hass.states.get(SOG).state == "5.0"
    after = registry.async_get(SOG)
    assert after.entity_id == before.entity_id
    assert after.unique_id == before.unique_id == f"{BOAT_ID}_sog_speed"
    sog_entities = [
        e
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.unique_id == f"{BOAT_ID}_sog_speed"
    ]
    assert len(sog_entities) == 1
    assert len(sensor_unique_ids(hass, entry)) == len(FIXED_UNIQUE_IDS) + 1


def test_fixed_list_matches_strings():
    """Every sensor translation without an instance is created at setup."""
    strings = json.loads((COMPONENT / "strings.json").read_text())
    fixed = {
        key
        for key, value in strings["entity"]["sensor"].items()
        if "{instance}" not in value["name"] and key != "last_update"
    }
    created = {description_for(key).translation_key for key in FIXED_VALUE_KEYS}
    assert created == fixed


def test_every_language_has_the_repair_issue():
    files = [COMPONENT / "strings.json", *(COMPONENT / "translations").glob("*.json")]
    assert len(files) >= 3
    for path in files:
        text = json.loads(path.read_text())["issues"]["hub_update_needed"]
        assert text["title"], path
        assert "1.49.0" in text["description"], path


async def test_one_notification_for_a_multi_instance_first_message(hass):
    entry = await setup_entry(hass)
    with patch(NOTIFY) as notify:
        FakeConnection.last.on_payload(
            make_message(
                (
                    22,
                    [
                        {0: {0: 12.6, 1: -3.5, 3: 80}, 1: {3: 0}},
                        {0: {0: 12.1}, 1: {3: 1}},
                    ],
                ),
                (24, [{0: 41, 1: {3: 0}}, {0: 20, 1: {3: 1}}, {0: 55, 1: {3: 2}}]),
            )
        )
        await hass.async_block_till_done()
    assert notify.call_count == 1
    args, kwargs = notify.call_args
    text = args[1]
    assert "Smartboatia boat a1b2c3" in text
    assert "Battery 0 (voltage" in text
    assert "Battery 1 (voltage)" in text
    assert "Fuel tank 0" in text and "Fuel tank 2" in text
    assert kwargs["notification_id"] == f"{DOMAIN}_{entry.entry_id}_new_sensors"


async def test_known_instances_do_not_notify_again_in_the_same_run(hass):
    await setup_entry(hass)
    payload = make_message((24, {0: 41, 1: {3: 0}}))
    with patch(NOTIFY) as notify:
        FakeConnection.last.on_payload(payload)
        await hass.async_block_till_done()
        FakeConnection.last.on_payload(payload)
        await hass.async_block_till_done()
    assert notify.call_count == 1


async def test_restart_with_registered_entities_does_not_notify(hass):
    entry = await setup_entry(hass)
    payload = make_message((22, {0: {0: 12.6}, 1: {3: 0}}), (24, {0: 41, 1: {3: 0}}))
    with patch(NOTIFY) as notify:
        FakeConnection.last.on_payload(payload)
        await hass.async_block_till_done()
    assert notify.call_count == 1
    registered = sensor_unique_ids(hass, entry)
    assert f"{BOAT_ID}_fuel_0_level" in registered

    with patch(NOTIFY) as notify, patch(CONN, FakeConnection):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        FakeConnection.last.on_payload(payload, True)
        await hass.async_block_till_done()
    assert hass.states.get("sensor.smartboatia_boat_a1b2c3_fuel_tank_0").state == "41.0"
    assert notify.call_count == 0
    assert sensor_unique_ids(hass, entry) == registered


async def test_legacy_payload_raises_repair_issue_until_v3_arrives(hass):
    entry = await setup_entry(hass)
    assert issue(hass, entry) is None
    FakeConnection.last.on_payload(LEGACY, True)
    await hass.async_block_till_done()
    found = issue(hass, entry)
    assert found is not None
    assert found.severity is ir.IssueSeverity.WARNING
    assert found.is_fixable is False
    assert found.translation_key == "hub_update_needed"
    assert entry.entry_id in found.issue_id
    assert len(ir.async_get(hass).issues) == 1
    # the fixed sensors are there, just without data
    assert hass.states.get(SOG).state == "unavailable"

    FakeConnection.last.on_payload(LEGACY)
    assert len(ir.async_get(hass).issues) == 1

    FakeConnection.last.on_payload(make_message((18, 5.0)))
    await hass.async_block_till_done()
    assert issue(hass, entry) is None
    assert hass.states.get(SOG).state == "5.0"


async def test_undecodable_or_empty_payload_does_not_raise_the_issue(hass):
    entry = await setup_entry(hass)
    FakeConnection.last.on_payload(b"\xa1\x00")  # looks like v3, does not decode
    FakeConnection.last.on_payload(b"")
    await hass.async_block_till_done()
    assert issue(hass, entry) is None


async def test_unload_and_removal_delete_the_issue(hass):
    entry = await setup_entry(hass)
    FakeConnection.last.on_payload(LEGACY)
    assert issue(hass, entry) is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert issue(hass, entry) is None

    with patch(CONN, FakeConnection):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    FakeConnection.last.on_payload(LEGACY)
    assert issue(hass, entry) is not None
    await hass.config_entries.async_remove(entry.entry_id)
    assert issue(hass, entry) is None


def test_discovery_text_names_groups_in_human_terms():
    text = discovery_text(
        "Boat X",
        [("fuel", "0", "level"), ("battery", "1", "voltage"), ("battery", "1", "soc")],
    )
    assert (
        text
        == "New sensors found on board of Boat X: Battery 1 (voltage, charge), Fuel tank 0."
    )
