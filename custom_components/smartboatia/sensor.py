"""Sensors of the boat.

Fixed values exist from setup on; instances (batteries, tanks, engines) appear
when first seen.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    DEGREE,
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfLength,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolumeFlowRate,
)
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from . import SmartboatiaConfigEntry
from .const import DOMAIN
from .coordinator import SmartboatiaCoordinator
from .entity import SmartboatiaEntity, boat_device_name
from .models import ValueKey
from .n_message import ENGINE_STATUS_1_BITS, ENGINE_STATUS_2_BITS, GEAR_NAMES

PARALLEL_UPDATES = 0

# LogEventType of the platform (the `log` measurement is the latest logbook event type)
BOAT_STATES = {
    0: "dwelling",
    1: "docked",
    2: "moored",
    3: "anchored",
    4: "motoring",
    5: "motor_sailing",
    6: "sailing",
    7: "moving",
    8: "tack",
    9: "gybe",
    10: "engine_on",
    11: "engine_off",
    12: "note",
    13: "alarm_event",
    14: "autopilot",
    15: "maintenance",
    16: "refueling",
}


def _flags(bits: dict[int, str]) -> Callable[[float], str]:
    def convert(value: float) -> str:
        names = sorted(name for bit, name in bits.items() if int(value) >> bit & 1)
        return ", ".join(names) if names else "none"

    return convert


@dataclass(frozen=True, kw_only=True)
class SmartboatiaSensorDescription(SensorEntityDescription):
    """Sensor description with an optional conversion of the decoded value."""

    value_fn: Callable[[float], StateType] | None = None


_M = SensorStateClass.MEASUREMENT
_ANGLE = SensorStateClass.MEASUREMENT_ANGLE
_DIAG = EntityCategory.DIAGNOSTIC


def _d(key: str, **kw) -> SmartboatiaSensorDescription:
    return SmartboatiaSensorDescription(key=key, translation_key=key, **kw)


# (measurement, field) -> base description; key and translation key may be refined
# per variant in ``description_for``.
DESCRIPTIONS: dict[tuple[str, str], SmartboatiaSensorDescription] = {
    ("voltage", "voltage"): _d(
        "board_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=_M,
    ),
    ("heading", "true_heading"): _d(
        "heading", native_unit_of_measurement=DEGREE, state_class=_ANGLE
    ),
    ("sea_roughness", "roughness"): _d("sea_roughness", state_class=_M),
    ("wind", "speed"): _d(
        "wind_speed",
        device_class=SensorDeviceClass.WIND_SPEED,
        native_unit_of_measurement=UnitOfSpeed.KNOTS,
        state_class=_M,
    ),
    ("wind", "direction"): _d(
        "wind_direction",
        device_class=SensorDeviceClass.WIND_DIRECTION,
        native_unit_of_measurement=DEGREE,
        state_class=_ANGLE,
    ),
    ("wind", "rel_angle"): _d(
        "wind_angle", native_unit_of_measurement=DEGREE, state_class=_ANGLE
    ),
    ("water_depth", "depth"): _d(
        "water_depth",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.METERS,
        state_class=_M,
    ),
    ("atmospheric_pressure", "pressure"): _d(
        "atmospheric_pressure",
        device_class=SensorDeviceClass.ATMOSPHERIC_PRESSURE,
        native_unit_of_measurement=UnitOfPressure.HPA,
        state_class=_M,
    ),
    ("stw", "speed"): _d(
        "speed_through_water",
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.KNOTS,
        state_class=_M,
    ),
    ("sog", "speed"): _d(
        "speed_over_ground",
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.KNOTS,
        state_class=_M,
    ),
    ("cog", "course"): _d(
        "course_over_ground", native_unit_of_measurement=DEGREE, state_class=_ANGLE
    ),
    ("water_temperature", "temperature"): _d(
        "water_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=_M,
    ),
    ("log", "event_type"): _d(
        "boat_state",
        device_class=SensorDeviceClass.ENUM,
        options=list(BOAT_STATES.values()),
        value_fn=lambda v: BOAT_STATES.get(int(v)),
    ),
    ("battery", "voltage"): _d(
        "battery_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=_M,
    ),
    ("battery", "current"): _d(
        "battery_current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=_M,
    ),
    ("battery", "temperature"): _d(
        "battery_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=_M,
    ),
    ("battery", "soc"): _d(
        "battery_charge",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=_M,
    ),
    ("battery", "soh"): _d(
        "battery_health",
        native_unit_of_measurement=PERCENTAGE,
        state_class=_M,
        entity_category=_DIAG,
    ),
    ("freshwater", "level"): _d(
        "freshwater_level", native_unit_of_measurement=PERCENTAGE, state_class=_M
    ),
    ("fuel", "level"): _d(
        "fuel_level", native_unit_of_measurement=PERCENTAGE, state_class=_M
    ),
    ("blackwater", "level"): _d(
        "blackwater_level", native_unit_of_measurement=PERCENTAGE, state_class=_M
    ),
    ("engine_n2k", "engine_speed"): _d(
        "engine_speed",
        native_unit_of_measurement=REVOLUTIONS_PER_MINUTE,
        state_class=_M,
    ),
    ("engine_n2k", "boost_pressure"): _d(
        "engine_boost_pressure",
        device_class=SensorDeviceClass.PRESSURE,
        native_unit_of_measurement=UnitOfPressure.BAR,
        state_class=_M,
    ),
    ("engine_n2k", "tilt_trim"): _d(
        "engine_tilt_trim", native_unit_of_measurement=PERCENTAGE, state_class=_M
    ),
    ("engine_n2k", "coolant_temperature"): _d(
        "engine_coolant_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=_M,
    ),
    ("engine_n2k", "oil_pressure"): _d(
        "engine_oil_pressure",
        device_class=SensorDeviceClass.PRESSURE,
        native_unit_of_measurement=UnitOfPressure.BAR,
        state_class=_M,
    ),
    ("engine_n2k", "oil_temperature"): _d(
        "engine_oil_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=_M,
    ),
    ("engine_n2k", "alternator_voltage"): _d(
        "engine_alternator_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=_M,
    ),
    ("engine_n2k", "fuel_rate"): _d(
        "engine_fuel_rate",
        device_class=SensorDeviceClass.VOLUME_FLOW_RATE,
        native_unit_of_measurement=UnitOfVolumeFlowRate.LITERS_PER_HOUR,
        state_class=_M,
    ),
    ("engine_n2k", "total_engine_hours"): _d(
        "engine_hours",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    ("engine_n2k", "fuel_pressure"): _d(
        "engine_fuel_pressure",
        device_class=SensorDeviceClass.PRESSURE,
        native_unit_of_measurement=UnitOfPressure.BAR,
        state_class=_M,
    ),
    ("engine_n2k", "coolant_pressure"): _d(
        "engine_coolant_pressure",
        device_class=SensorDeviceClass.PRESSURE,
        native_unit_of_measurement=UnitOfPressure.BAR,
        state_class=_M,
    ),
    ("engine_n2k", "engine_load"): _d(
        "engine_load", native_unit_of_measurement=PERCENTAGE, state_class=_M
    ),
    ("engine_n2k", "engine_torque"): _d(
        "engine_torque", native_unit_of_measurement=PERCENTAGE, state_class=_M
    ),
    ("engine_n2k", "discrete_status_1"): _d(
        "engine_status_1",
        entity_category=_DIAG,
        value_fn=_flags(ENGINE_STATUS_1_BITS),
    ),
    ("engine_n2k", "discrete_status_2"): _d(
        "engine_status_2",
        entity_category=_DIAG,
        value_fn=_flags(ENGINE_STATUS_2_BITS),
    ),
    ("transmission", "gear"): _d(
        "transmission_gear",
        device_class=SensorDeviceClass.ENUM,
        options=list(GEAR_NAMES.values()),
        value_fn=lambda v: GEAR_NAMES.get(int(v)),
    ),
    ("transmission", "oil_pressure"): _d(
        "transmission_oil_pressure",
        device_class=SensorDeviceClass.PRESSURE,
        native_unit_of_measurement=UnitOfPressure.BAR,
        state_class=_M,
    ),
    ("transmission", "oil_temperature"): _d(
        "transmission_oil_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=_M,
    ),
}

_INSTANCE_MEASUREMENTS = {
    "battery",
    "freshwater",
    "fuel",
    "blackwater",
    "engine_n2k",
    "transmission",
}


def description_for(key: ValueKey) -> SmartboatiaSensorDescription | None:
    """Return the description for a value key, None if it has no sensor."""
    measurement, variant, name = key
    base = DESCRIPTIONS.get((measurement, name))
    if base is None:
        return None
    translation_key = base.translation_key
    if measurement == "wind":
        # the decoder names the apparent direction ``rel_angle``, nothing else does
        if (variant == "apparent" and name == "direction") or (
            variant != "apparent" and name == "rel_angle"
        ):
            return None
        translation_key = f"{translation_key}_{variant}"
    elif measurement == "cog" and variant == "magnetic":
        translation_key = "course_over_ground_magnetic"
    elif measurement == "cog" and variant != "true":
        return None
    return replace(
        base,
        key="_".join(part for part in key if part),
        translation_key=translation_key,
    )


# Variants (wind reference, course orientation) of the values without an instance
# number. The only hand-maintained list: the rest of ``FIXED_VALUE_KEYS`` follows
# ``DESCRIPTIONS``, and a test checks it against ``strings.json``.
_FIXED_VARIANTS: dict[str, tuple[str, ...]] = {
    "wind": ("ground", "true", "apparent"),
    "cog": ("true", "magnetic"),
}


def _fixed_value_keys() -> tuple[ValueKey, ...]:
    keys = (
        (measurement, variant, name)
        for measurement, name in DESCRIPTIONS
        if measurement not in _INSTANCE_MEASUREMENTS
        for variant in _FIXED_VARIANTS.get(measurement, ("",))
    )
    return tuple(key for key in keys if description_for(key) is not None)


# Values without an instance number: created at setup, unavailable until they arrive.
FIXED_VALUE_KEYS = _fixed_value_keys()

# group of an instance sensor -> name used in the "new sensors" notification
_GROUP_NAMES = {
    "battery": "Battery",
    "freshwater": "Fresh water tank",
    "fuel": "Fuel tank",
    "blackwater": "Black water tank",
    "engine_n2k": "Engine",
    "transmission": "Transmission",
}
_GROUP_ORDER = list(_GROUP_NAMES)


def _detail(description: SmartboatiaSensorDescription) -> str:
    """Short name of a measurement within its group ("charge", "oil pressure")."""
    assert description.translation_key is not None
    _, _, detail = description.translation_key.partition("_")
    return "" if detail == "level" else detail.replace("_", " ")


def discovery_text(boat_name: str, found: list[ValueKey]) -> str:
    """Human description of newly discovered instance sensors."""
    groups: dict[tuple[str, str], list[str]] = {}
    for key in found:
        description = description_for(key)
        if description is not None:
            detail = _detail(description)
            groups.setdefault(key[:2], []).append(detail)
    parts = []
    for (measurement, instance), details in sorted(
        groups.items(), key=lambda item: (_GROUP_ORDER.index(item[0][0]), item[0][1])
    ):
        label = f"{_GROUP_NAMES[measurement]} {instance}"
        parts.append(
            f"{label} ({', '.join(d for d in details if d)})" if any(details) else label
        )
    return f"New sensors found on board of {boat_name}: {', '.join(parts)}."


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartboatiaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the fixed sensors now and the instance sensors as they show up."""
    coordinator = entry.runtime_data
    registry = er.async_get(hass)
    known: set[ValueKey] = set()
    fixed: list[SmartboatiaSensor] = []
    for key in FIXED_VALUE_KEYS:
        known.add(key)
        description = description_for(key)
        assert description is not None
        fixed.append(SmartboatiaSensor(coordinator, description, key))

    @callback
    def _add_new() -> None:
        new: list[SmartboatiaSensor] = []
        unseen: list[ValueKey] = []
        for key in coordinator.data.values:
            if key in known:
                continue
            known.add(key)
            description = description_for(key)
            if description is None:
                continue
            sensor = SmartboatiaSensor(coordinator, description, key)
            new.append(sensor)
            if (
                registry.async_get_entity_id("sensor", DOMAIN, sensor.unique_id or "")
                is None
            ):
                unseen.append(key)
        if new:
            async_add_entities(new)
        if unseen:
            # one notification per batch; the id is stable per entry, so a later
            # discovery replaces the text instead of piling up another one
            persistent_notification.async_create(
                hass,
                discovery_text(boat_device_name(coordinator.boat_id), unseen),
                title="Smartboatia",
                notification_id=f"{DOMAIN}_{entry.entry_id}_new_sensors",
            )

    async_add_entities([SmartboatiaLastUpdate(coordinator), *fixed])
    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class SmartboatiaSensor(SmartboatiaEntity, SensorEntity):
    """A measurement of the boat."""

    entity_description: SmartboatiaSensorDescription

    def __init__(
        self,
        coordinator: SmartboatiaCoordinator,
        description: SmartboatiaSensorDescription,
        value_key: ValueKey,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._value_key = value_key
        self._attr_unique_id = f"{coordinator.boat_id}_{description.key}"
        measurement, variant, _ = value_key
        if measurement in _INSTANCE_MEASUREMENTS:
            self._attr_translation_placeholders = {"instance": variant}

    @property
    def available(self) -> bool:
        """A value that is missing from the message is stale, not zero."""
        return self._value_key in self.coordinator.data.values

    @property
    def native_value(self) -> StateType:
        """Return the value."""
        value = self.coordinator.data.values.get(self._value_key)
        if value is None:
            return None
        convert = self.entity_description.value_fn
        return convert(value) if convert else value


class SmartboatiaLastUpdate(SmartboatiaEntity, SensorEntity):
    """When the boat's latest message was created (shows how old the data is)."""

    _attr_translation_key = "last_update"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SmartboatiaCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.boat_id}_last_update"

    @property
    def available(self) -> bool:
        """Available once a message was received, also when its data is old."""
        return self.coordinator.data.timestamp is not None

    @property
    def native_value(self) -> datetime | None:
        """Creation time of the latest message."""
        return self.coordinator.data.timestamp
