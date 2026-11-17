"""Own MQTT connection to the Smartboatia cloud broker.

Not Home Assistant's mqtt integration. The connection is read-only (it never
publishes), subscribes to exactly the topic the token exchange returned, and keeps
itself alive across the one-hour token lifetime:

* the deadline of a token is ``local receive time + expires_in`` on the monotonic
  clock, never the wall clock;
* shortly before it (``min(RENEW_MARGIN_SECONDS, expires_in / 2)``) a successor is
  minted first, then the connection is rebuilt with it; the retained message arrives
  again right after the subscribe, so nothing is lost;
* an unexpected disconnect (the broker kicks at expiry) reconnects, reusing the token
  while it is still good and minting a new one otherwise;
* the cloud allows 20 exchanges per key per hour: every failure path backs off
  exponentially, the backoff only starts over after a link was up and subscribed for
  ``STABLE_AFTER_SECONDS``, and two exchanges are never closer than
  ``MIN_MINT_INTERVAL_SECONDS`` (at most 15 per hour whatever fails);
* nothing raised inside the loop can end it: unexpected errors are logged (type only,
  never a message that could carry a secret), the connection is marked unavailable and
  retried with backoff.

The API key and the tokens are never logged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
import random
import time
from typing import Any
import uuid

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.util.ssl import client_context

from .api import (
    InvalidAuth,
    MqttToken,
    NoSubscription,
    NotBoatOwner,
    RateLimited,
    TemporarilyUnavailable,
    async_mint_token,
)
from .const import (
    BACKOFF_MAX_SECONDS,
    BACKOFF_MIN_SECONDS,
    CONNECT_TIMEOUT_SECONDS,
    MINT_BACKOFF_MIN_SECONDS,
    MQTT_HOST,
    MQTT_KEEPALIVE,
    MQTT_PORT,
    MIN_MINT_INTERVAL_SECONDS,
    MQTT_TLS,
    NO_SUBSCRIPTION_WAIT_SECONDS,
    RATE_LIMIT_MIN_WAIT_SECONDS,
    RENEW_MARGIN_SECONDS,
    RENEW_RETRY_SECONDS,
    REUSE_MARGIN_SECONDS,
    STABLE_AFTER_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_DISCONNECTED = "disconnected"
STATE_NO_SUBSCRIPTION = "no_subscription"
STATE_RATE_LIMITED = "rate_limited"
STATE_UNAVAILABLE = "unavailable"
STATE_AUTH_FAILED = "auth_failed"
STATE_STOPPED = "stopped"

MAX_RETRY_AFTER_SECONDS = 3600.0


def backoff_delay(
    failures: int,
    minimum: float = BACKOFF_MIN_SECONDS,
    maximum: float = BACKOFF_MAX_SECONDS,
    rng: Callable[[], float] = random.random,
) -> float:
    """Exponential backoff with jitter (+-25 %), capped at ``maximum``."""
    base = min(maximum, minimum * 2 ** max(0, failures - 1))
    return base * (0.75 + 0.5 * rng())


def renew_delay(
    token: MqttToken, now: float, margin: float = RENEW_MARGIN_SECONDS
) -> float:
    """Seconds until the token has to be renewed (0 if that is already due).

    The margin is capped at half the lifetime, so a short-lived token is not renewed
    the moment it arrives.
    """
    margin = min(margin, token.expires_in / 2)
    return max(0.0, token.expires_at_monotonic - margin - now)


def is_reusable(
    token: MqttToken, now: float, margin: float = REUSE_MARGIN_SECONDS
) -> bool:
    """Whether a token is still good enough for a (re)connect."""
    margin = min(margin, token.expires_in / 4)
    return token.expires_at_monotonic - now > margin


class PahoLink:

    """One paho connection made with one token. Replaced, never reused."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        token: MqttToken,
        on_payload: Callable[[bytes, bool], None],
    ) -> None:
        """Prepare the link; ``start()`` connects."""
        import paho.mqtt.client as mqtt  # noqa: PLC0415

        self._loop = loop
        self._topic = token.topic
        self._on_payload = on_payload
        self.connected = asyncio.Event()
        self.lost = asyncio.Event()
        self.auth_rejected = False

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"ha-smartboatia-{uuid.uuid4().hex[:12]}",
            protocol=mqtt.MQTTv311,
        )
        client.username_pw_set(token.username, token.password)
        if MQTT_TLS:
            client.tls_set_context(client_context())
        # the manager owns reconnecting (with fresh credentials); keep paho from
        # racing it with stale ones
        client.reconnect_delay_set(min_delay=60, max_delay=120)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.on_subscribe = self._on_subscribe
        self._client = client

    def start(self) -> None:
        """Connect in the background (paho network thread)."""
        self._client.connect_async(MQTT_HOST, MQTT_PORT, MQTT_KEEPALIVE)
        self._client.loop_start()

    async def stop(self) -> None:
        """Disconnect and stop the network thread."""
        await self._loop.run_in_executor(None, self._stop_sync)

    def _stop_sync(self) -> None:
        try:
            self._client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self._client.loop_stop()

    # callbacks run on the paho thread: only hand over to the event loop
    def _signal(self, event: asyncio.Event) -> None:
        self._loop.call_soon_threadsafe(event.set)

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            # 4 / 5 (MQTT 3.1.1) or 134 / 135: the broker refused the credentials
            self.auth_rejected = reason_code.value in (4, 5, 134, 135)
            self._signal(self.lost)
            return
        # exactly the granted topic, read-only; the link counts as connected once the
        # broker has granted the subscription
        client.subscribe(self._topic, qos=0)

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties) -> None:
        if any(getattr(code, "is_failure", False) for code in reason_codes):
            # denied SUBACK: a failed connection, not a healthy one
            self._signal(self.lost)
            return
        self._signal(self.connected)

    def _on_disconnect(
        self, client, userdata, flags, reason_code, properties=None
    ) -> None:
        self._signal(self.lost)

    def _on_message(self, client, userdata, message) -> None:
        if message.topic != self._topic:
            return
        payload = bytes(message.payload)
        # the retain flag tells a replay of the stored message (after a subscribe)
        # from a message that was just published
        retained = bool(message.retain)
        self._loop.call_soon_threadsafe(self._on_payload, payload, retained)


class SmartboatiaConnection:
    """Keeps the broker connection alive; hands payloads to ``on_payload``."""

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession | None,
        api_key: str,
        *,
        on_payload: Callable[[bytes, bool], None],
        on_state: Callable[[str], None],
        on_auth_failed: Callable[[], None],
        first_token: MqttToken | None = None,
        mint: Callable[[], Any] | None = None,
        link_factory: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
        renew_margin: float = RENEW_MARGIN_SECONDS,
        reuse_margin: float = REUSE_MARGIN_SECONDS,
        renew_retry: float = RENEW_RETRY_SECONDS,
        backoff_min: float = BACKOFF_MIN_SECONDS,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        stable_after: float = STABLE_AFTER_SECONDS,
        min_mint_interval: float = MIN_MINT_INTERVAL_SECONDS,
    ) -> None:
        """Create the connection (``async_start`` runs it)."""
        self._hass = hass
        self._session = session
        self._api_key = api_key
        self._on_payload = on_payload
        self._on_state = on_state
        self._on_auth_failed = on_auth_failed
        self._mint = mint or self._mint_default
        self._link_factory = link_factory or PahoLink
        self._clock = clock
        self._sleep = sleep
        self._renew_margin = renew_margin
        self._reuse_margin = reuse_margin
        self._renew_retry = renew_retry
        self._backoff_min = backoff_min
        self._connect_timeout = connect_timeout
        self._stable_after = stable_after
        self._min_mint_interval = min_mint_interval
        self._task: asyncio.Task | None = None
        self._link: Any = None
        self._token: MqttToken | None = first_token
        self._last_mint: float | None = None
        self._failures = 0
        self.state = STATE_STOPPED
        self.mints = 0
        self.renewals = 0
        self.connects = 0

    async def _mint_default(self) -> MqttToken:
        return await async_mint_token(self._session, self._api_key, clock=self._clock)

    def diagnostics(self) -> dict[str, Any]:
        """Non-secret state for diagnostics."""
        token = self._token
        return {
            "state": self.state,
            "mints": self.mints,
            "renewals": self.renewals,
            "connects": self.connects,
            "failures": self._failures,
            "token_seconds_left": (
                round(token.expires_at_monotonic - self._clock())
                if token is not None
                else None
            ),
        }

    def async_start(self) -> None:
        """Start the connection task."""
        if self._task is None:
            self._task = self._hass.loop.create_task(self._run())

    async def async_stop(self) -> None:
        """Stop the task and close the link (safe to call more than once)."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass  # whatever ended the task was logged there
        link, self._link = self._link, None
        await self._close(link)
        self._set_state(STATE_STOPPED)

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self._on_state(state)

    async def _close(self, link: Any) -> None:
        if link is not None:
            try:
                await link.stop()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Closing the MQTT link failed")

    async def _first_of(self, link: Any, delay: float, *events: str) -> set[str]:
        """Wait for the named link events or ``delay`` s; return those that are set."""
        waiters = {
            name: asyncio.ensure_future(getattr(link, name).wait()) for name in events
        }
        timer = asyncio.ensure_future(self._sleep(delay))
        try:
            await asyncio.wait(
                {*waiters.values(), timer}, return_when=asyncio.FIRST_COMPLETED
            )
            return {name for name, fut in waiters.items() if fut.done()}
        finally:
            timer.cancel()
            for fut in waiters.values():
                fut.cancel()

    async def _sleep_or_lost(self, link: Any, delay: float) -> bool:
        """Sleep ``delay`` seconds; return True if the link was lost meanwhile."""
        return "lost" in await self._first_of(link, delay, "lost")

    async def _wait_connected(self, link: Any) -> bool:
        """Wait for the link to be connected and subscribed."""
        done = await self._first_of(link, self._connect_timeout, "connected", "lost")
        return "connected" in done and "lost" not in done

    def _auth_failed(self) -> None:
        self._set_state(STATE_AUTH_FAILED)
        self._on_auth_failed()

    def _mint_wait(self) -> float:
        """Seconds to wait so that two exchanges are never too close together."""
        if self._last_mint is None:
            return 0.0
        return max(0.0, self._last_mint + self._min_mint_interval - self._clock())

    async def _do_mint(self) -> MqttToken:
        self._last_mint = self._clock()
        self.mints += 1
        return await self._mint()

    async def _mint_until_success(self) -> MqttToken | None:
        """Mint a token, backing off on every failure. None on an auth failure."""
        failures = 0
        while True:
            wait = self._mint_wait()
            if wait > 0:
                await self._sleep(wait)
            try:
                return await self._do_mint()
            except (InvalidAuth, NotBoatOwner):
                self._auth_failed()
                return None
            except NoSubscription:
                _LOGGER.warning("The boat has no active Smartboatia subscription")
                self._set_state(STATE_NO_SUBSCRIPTION)
                wait = NO_SUBSCRIPTION_WAIT_SECONDS
            except RateLimited as err:
                _LOGGER.warning("Smartboatia token exchange is rate limited")
                self._set_state(STATE_RATE_LIMITED)
                wait = max(
                    RATE_LIMIT_MIN_WAIT_SECONDS,
                    min(err.retry_after or 0, MAX_RETRY_AFTER_SECONDS),
                )
            except TemporarilyUnavailable:
                failures += 1
                _LOGGER.warning("Smartboatia token exchange temporarily unavailable")
                self._set_state(STATE_UNAVAILABLE)
                wait = backoff_delay(failures, MINT_BACKOFF_MIN_SECONDS)
            await self._sleep(wait)

    async def _renew(self, link: Any, token: MqttToken) -> MqttToken | None:
        """Wait until renewal is due, then mint the successor.

        Returns the successor, or None if the link was lost or the token ran out
        first (the main loop then reconnects).
        """
        if await self._sleep_or_lost(
            link, renew_delay(token, self._clock(), self._renew_margin)
        ):
            return None
        while self._clock() < token.expires_at_monotonic:
            wait = self._mint_wait()
            if wait > 0 and await self._sleep_or_lost(link, wait):
                return None
            wait = self._renew_retry
            try:
                successor = await self._do_mint()
            except (InvalidAuth, NotBoatOwner):
                self._auth_failed()
                raise
            except RateLimited as err:
                wait = max(wait, min(err.retry_after or 0, MAX_RETRY_AFTER_SECONDS))
            except (NoSubscription, TemporarilyUnavailable):
                pass
            else:
                self.renewals += 1
                return successor
            _LOGGER.warning("Renewing the Smartboatia token failed, keeping the link")
            if await self._sleep_or_lost(link, wait):
                return None
        return None

    async def _run(self) -> None:
        """Run connection cycles until stopped or the key is rejected."""
        while True:
            try:
                if not await self._cycle():
                    return
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                # type name only: messages of library errors may carry URLs or data
                _LOGGER.warning(
                    "Unexpected error in the Smartboatia connection (%s), retrying",
                    type(err).__name__,
                )
                link, self._link = self._link, None
                await self._close(link)
                self._set_state(STATE_UNAVAILABLE)
                self._failures += 1
                await self._sleep(backoff_delay(self._failures, self._backoff_min))

    async def _cycle(self) -> bool:
        """One connection: mint if needed, connect, hold, renew or lose.

        Returns False when the loop must end (the key was rejected).
        """
        token = self._token
        if token is None or not is_reusable(token, self._clock(), self._reuse_margin):
            token = await self._mint_until_success()
            if token is None:
                self._token = None
                return False
        self._token = token
        self._set_state(STATE_CONNECTING)
        link = self._link_factory(self._hass.loop, token, self._on_payload)
        self._link = link
        self.connects += 1
        link.start()
        if not await self._wait_connected(link):
            # broker down, credentials rejected or subscription denied
            await self._close(link)
            self._link = None
            if link.auth_rejected:
                self._token = None
            self._failures += 1
            self._set_state(STATE_DISCONNECTED)
            await self._sleep(backoff_delay(self._failures, self._backoff_min))
            return True

        self._set_state(STATE_CONNECTED)
        up_since = self._clock()
        try:
            successor = await self._renew(link, token)
        except (InvalidAuth, NotBoatOwner):
            await self._close(link)
            self._link = None
            return False
        await self._close(link)
        self._link = None
        if successor is not None:
            self._token = successor
            return True
        # lost or expired: back off, and only a link that was healthy for a while
        # resets the escalation (a kick right after connect must not reconnect fast)
        if self._clock() - up_since >= self._stable_after:
            self._failures = 0
        else:
            self._failures += 1
        self._set_state(STATE_DISCONNECTED)
        await self._sleep(backoff_delay(max(1, self._failures), self._backoff_min))
        return True
