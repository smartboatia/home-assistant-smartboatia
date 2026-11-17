"""Push-based coordinator: the MQTT connection feeds it, nothing is polled."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .connection import SmartboatiaConnection
from .const import (
    CONF_API_KEY,
    DOMAIN,
    STALE_AFTER_SECONDS,
    STALE_CHECK_INTERVAL_SECONDS,
)
from .models import BoatSnapshot, build_snapshot
from .n_message import NMessageError, decode_n_message, is_v3

_LOGGER = logging.getLogger(__name__)


def _now() -> datetime:
    """Current UTC time (a seam for tests)."""
    return dt_util.utcnow()


def _monotonic() -> float:
    """Monotonic seconds (a seam for tests)."""
    return time.monotonic()


class SmartboatiaCoordinator(DataUpdateCoordinator[BoatSnapshot]):
    """Holds the latest boat snapshot and the broker connection."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        boat_id: str,
        *,
        connection_factory=None,
    ) -> None:
        """Create the coordinator (``async_start`` connects)."""
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN)
        self.boat_id = boat_id
        self.data = BoatSnapshot()
        self.connection_state = "connecting"
        self._last_received: float | None = None
        self._seen_message = False
        self.connection = (connection_factory or SmartboatiaConnection)(
            hass,
            async_get_clientsession(hass),
            entry.data[CONF_API_KEY],
            on_payload=self.handle_payload,
            on_state=self._on_state,
            on_auth_failed=self._on_auth_failed,
        )

    async def _async_update_data(self) -> BoatSnapshot:
        return self.data

    @callback
    def async_start(self) -> None:
        """Start the connection, the stale check and the shutdown hook."""
        self.connection.async_start()
        self.config_entry.async_on_unload(
            async_track_time_interval(
                self.hass,
                self._check_stale,
                timedelta(seconds=STALE_CHECK_INTERVAL_SECONDS),
            )
        )
        self.config_entry.async_on_unload(
            self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, self._async_on_ha_stop
            )
        )

    async def _async_on_ha_stop(self, _event: Event) -> None:
        """Disconnect cleanly at shutdown."""
        await self.async_stop()

    async def async_stop(self) -> None:
        """Stop the connection."""
        await self.connection.async_stop()

    @callback
    def handle_payload(self, payload: bytes, retained: bool = False) -> None:
        """Handle one message from the broker.

        Args:
            payload: The raw message.
            retained: The MQTT retain flag. The broker replays the retained message
                on every subscribe (also at every hourly reconnect), so a retained
                message says nothing about the boat being online now.
        """
        if not is_v3(payload):
            _LOGGER.debug("Ignoring a message that is not N-message v3")
            return
        if retained and self._seen_message:
            # A replay after a reconnect: never refreshes the liveness timer and
            # never revives values that went stale (the data is either the message
            # we already have or an older one).
            _LOGGER.debug("Ignoring a replayed retained message")
            return
        try:
            message = decode_n_message(payload)
        except NMessageError as err:
            _LOGGER.debug("Discarding an invalid N-message (%s)", err)
            return
        self._seen_message = True
        snapshot = build_snapshot(message)
        # A message older than the stale limit only updates the "last update"
        # timestamp (so a Hub clock more than the stale limit behind real time shows
        # its data as old, see the README). A timestamp in the future (a Hub clock that
        # runs ahead) is accepted: it cannot be told from a live message.
        age = (_now() - message.timestamp).total_seconds()
        if age > STALE_AFTER_SECONDS:
            _LOGGER.debug("Message is %d s old, not treating it as live", int(age))
            snapshot.values = {}
        elif retained:
            # first value after (re)start from the retained message: it is only as
            # live as its age says, so the liveness window counts from its creation
            self._last_received = _monotonic() - max(0.0, age)
        else:
            self._last_received = _monotonic()
        self.async_set_updated_data(snapshot)

    @callback
    def _check_stale(self, _now: datetime) -> None:
        """Values older than the stale limit become unavailable, never zero."""
        if (
            self._last_received is not None
            and self.data.values
            and _monotonic() - self._last_received > STALE_AFTER_SECONDS
        ):
            _LOGGER.debug("No message for a while, marking values stale")
            self.async_set_updated_data(
                BoatSnapshot(self.data.timestamp, self.data.origin, self.data.rssi)
            )

    @callback
    def _on_state(self, state: str) -> None:
        self.connection_state = state
        _LOGGER.debug("Connection state: %s", state)

    @callback
    def _on_auth_failed(self) -> None:
        _LOGGER.warning("The Smartboatia API key was rejected, reauthentication needed")
        self.config_entry.async_start_reauth(self.hass)
