"""GPS position of the boat."""

from __future__ import annotations

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SmartboatiaConfigEntry
from .coordinator import SmartboatiaCoordinator
from .entity import SmartboatiaEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartboatiaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the boat tracker."""
    async_add_entities([SmartboatiaTracker(entry.runtime_data)])


class SmartboatiaTracker(SmartboatiaEntity, TrackerEntity):
    """Position of the boat."""

    _attr_translation_key = "position"
    _attr_source_type = SourceType.GPS

    def __init__(self, coordinator: SmartboatiaCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.boat_id}_position"

    @property
    def available(self) -> bool:
        """Available while a position is known."""
        return self.latitude is not None and self.longitude is not None

    @property
    def latitude(self) -> float | None:
        """Latitude."""
        return self.coordinator.data.values.get(("gps_pos", "", "lat"))

    @property
    def longitude(self) -> float | None:
        """Longitude."""
        return self.coordinator.data.values.get(("gps_pos", "", "long"))
