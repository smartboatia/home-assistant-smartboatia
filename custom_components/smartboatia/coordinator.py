"""Push-based coordinator: the MQTT connection feeds it, nothing is polled."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import MqttToken
from .connection import SmartboatiaConnection
from .const import CONF_API_KEY, DOMAIN, STALE_AFTER_SECONDS, STALE_CHECK_INTERVAL_SECONDS
from .models import BoatSnapshot, build_snapshot
from .n_message import NMessageError, decode_n_message, is_v3

_LOGGER = logging.getLogger(__name__)


class SmartboatiaCoordinator(DataUpdateCoordinator[BoatSnapshot]):
    """Holds the latest boat snapshot and the broker connection."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        boat_id: str,
        first_token: MqttToken,
        *,
        connection_factory=None,
    ) -> None:
        """Create the coordinator (``async_start`` connects)."""
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN)
        self.boat_id = boat_id
        self.data = BoatSnapshot()
        self.connection_state = "connecting"
        self._last_received: float | None = None
        self.connection = (connection_factory or SmartboatiaConnection)(
            hass,
            async_get_clientsession(hass),
            entry.data[CONF_API_KEY],
            on_payload=self.handle_payload,
            on_state=self._on_state,
            on_auth_failed=self._on_auth_failed,
            first_token=first_token,
        )

    async def _async_update_data(self) -> BoatSnapshot:
        return self.data

    @callback
    def async_start(self) -> None:
        """Start the connection and the stale check."""
        self.connection.async_start()
        self.config_entry.async_on_unload(
            async_track_time_interval(
                self.hass,
                self._check_stale,
                timedelta(seconds=STALE_CHECK_INTERVAL_SECONDS),
            )
        )

    async def async_stop(self) -> None:
        """Stop the connection."""
        await self.connection.async_stop()

    @callback
    def handle_payload(self, payload: bytes) -> None:
        """Handle one message from the broker."""
        if not is_v3(payload):
            _LOGGER.debug("Ignoring a message that is not N-message v3")
            return
        try:
            message = decode_n_message(payload)
        except NMessageError as err:
            _LOGGER.debug("Discarding an invalid N-message (%s)", err)
            return
        self._last_received = time.monotonic()
        self.async_set_updated_data(build_snapshot(message))

    @callback
    def _check_stale(self, _now: datetime) -> None:
        """Values older than the stale limit become unavailable, never zero."""
        if (
            self._last_received is not None
            and self.data.values
            and time.monotonic() - self._last_received > STALE_AFTER_SECONDS
        ):
            _LOGGER.debug("No message for a while, marking values stale")
            self.async_set_updated_data(BoatSnapshot())

    @callback
    def _on_state(self, state: str) -> None:
        self.connection_state = state
        _LOGGER.debug("Connection state: %s", state)

    @callback
    def _on_auth_failed(self) -> None:
        _LOGGER.warning("The Smartboatia API key was rejected, reauthentication needed")
        self.config_entry.async_start_reauth(self.hass)
