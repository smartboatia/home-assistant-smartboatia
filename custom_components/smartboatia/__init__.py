"""The Smartboatia integration: boat status from the Smartboatia cloud."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .coordinator import SmartboatiaCoordinator, hub_update_issue_id

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.DEVICE_TRACKER, Platform.SENSOR]

type SmartboatiaConfigEntry = ConfigEntry[SmartboatiaCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: SmartboatiaConfigEntry) -> bool:
    """Set up Smartboatia from a config entry.

    No token is minted here: the connection does the first exchange itself, with its
    backoff (the cloud allows only 20 exchanges per key and hour, so retrying through
    Home Assistant's setup retry would exceed it). A rejected key starts reauth.
    """
    if entry.unique_id is None:
        raise ConfigEntryError("The entry has no boat id, please add it again")
    coordinator = SmartboatiaCoordinator(hass, entry, entry.unique_id)
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SmartboatiaConfigEntry) -> bool:
    """Unload the entry and close the broker connection."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        # stop first: a payload still in flight must not raise the issue again
        await entry.runtime_data.async_stop()
        entry.runtime_data.clear_hub_update_issue()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: SmartboatiaConfigEntry) -> None:
    """Drop the repair issue of a removed entry (also if it never loaded)."""
    ir.async_delete_issue(hass, DOMAIN, hub_update_issue_id(entry.entry_id))
