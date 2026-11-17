"""The Smartboatia integration: boat status from the Smartboatia cloud."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    InvalidAuth,
    NoSubscription,
    NotBoatOwner,
    RateLimited,
    TemporarilyUnavailable,
    async_mint_token,
)
from .const import CONF_API_KEY, DOMAIN
from .coordinator import SmartboatiaCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.DEVICE_TRACKER, Platform.SENSOR]

type SmartboatiaConfigEntry = ConfigEntry[SmartboatiaCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: SmartboatiaConfigEntry) -> bool:
    """Set up Smartboatia from a config entry."""
    session = async_get_clientsession(hass)
    try:
        token = await async_mint_token(session, entry.data[CONF_API_KEY])
    except (InvalidAuth, NotBoatOwner) as err:
        raise ConfigEntryAuthFailed("The API key was rejected") from err
    except NoSubscription as err:
        raise ConfigEntryNotReady("The boat has no active subscription") from err
    except (RateLimited, TemporarilyUnavailable) as err:
        raise ConfigEntryNotReady(
            "The Smartboatia cloud is temporarily unavailable"
        ) from err

    coordinator = SmartboatiaCoordinator(hass, entry, token.boat_id, token)
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SmartboatiaConfigEntry) -> bool:
    """Unload the entry and close the broker connection."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded
