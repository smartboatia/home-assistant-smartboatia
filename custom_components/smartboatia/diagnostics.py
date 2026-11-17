"""Diagnostics for Smartboatia."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import SmartboatiaConfigEntry
from .const import CONF_API_KEY

# the boat position and the boat id are redacted as well (keys of the flattened
# values are "<measurement>/<variant>/<field>")
TO_REDACT = {
    CONF_API_KEY,
    "username",
    "password",
    "token",
    "topic",
    "boat_id",
    "gps_pos//lat",
    "gps_pos//long",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SmartboatiaConfigEntry
) -> dict[str, Any]:
    """Return diagnostics with all secrets and the boat id redacted."""
    coordinator = entry.runtime_data
    snapshot = coordinator.data
    return {
        "entry": async_redact_data(
            {"title": entry.title, "unique_id": entry.unique_id, **entry.data},
            TO_REDACT | {"unique_id"},
        ),
        "connection": async_redact_data(coordinator.connection.diagnostics(), TO_REDACT),
        "last_message": async_redact_data(snapshot.as_dict(), TO_REDACT),
    }
