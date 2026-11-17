"""Base entity: one Home Assistant device per boat."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SmartboatiaCoordinator


def short_boat_id(boat_id: str) -> str:
    """Short, readable part of the boat id that tells two boats apart."""
    return boat_id[-6:]


class SmartboatiaEntity(CoordinatorEntity[SmartboatiaCoordinator]):
    """Entity of the boat device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SmartboatiaCoordinator) -> None:
        """Set up the device info."""
        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.boat_id)},
            manufacturer="Smartboatia",
            name=f"Smartboatia boat {short_boat_id(coordinator.boat_id)}",
            model="Boat",
        )
