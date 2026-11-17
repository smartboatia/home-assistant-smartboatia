"""Client for the Smartboatia cloud MQTT token exchange.

The API key and the token are secrets: nothing in this module logs or formats them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from typing import Any

import aiohttp

from .const import (
    API_BASE_URL,
    MAX_EXPIRES_IN_SECONDS,
    MIN_EXPIRES_IN_SECONDS,
    REQUEST_TIMEOUT,
    TOKEN_PATH,
)


class SmartboatiaError(Exception):
    """Base class of the token exchange errors (messages never carry secrets)."""


class InvalidAuth(SmartboatiaError):
    """401: key invalid, unknown, revoked or expired."""


class NotBoatOwner(SmartboatiaError):
    """403: the creator of the key is no longer owner of the boat."""


class NoSubscription(SmartboatiaError):
    """402: the boat has no active subscription."""


class RateLimited(SmartboatiaError):
    """429: too many exchanges for this key."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


class TemporarilyUnavailable(SmartboatiaError):
    """503, network error, timeout or an unusable answer."""


@dataclass(frozen=True)
class MqttToken:
    """Short-lived MQTT credentials.

    Attributes:
        username: MQTT username (names the key, secret).
        password: MQTT password, a JWT (secret).
        expires_in: lifetime in seconds from the moment the answer was received.
        topic: the only topic the token may read, e.g. ``<boat>/n``.
        received_at: monotonic time at which the request was started.
    """

    username: str
    password: str
    expires_in: float
    topic: str
    received_at: float

    def __repr__(self) -> str:
        """Never show credentials."""
        return f"MqttToken(expires_in={self.expires_in})"

    __str__ = __repr__

    @property
    def expires_at_monotonic(self) -> float:
        """Deadline on the monotonic clock (local receive time plus lifetime)."""
        return self.received_at + self.expires_in

    @property
    def boat_id(self) -> str:
        """The boat topic id (topic without the trailing ``/n``)."""
        return self.topic.removesuffix("/n")


def parse_token_response(body: Any, received_at: float) -> MqttToken:
    """Parse the DISPATCH answer.

    The answer is the usual envelope (``v``, ``t``, ``status``, ``status_description``)
    with ``credentials`` {``username``, ``password``}, ``expires_at``, ``expires_in``
    and ``topic`` next to it. Only ``expires_in`` is used for timing (not the wall
    clock ``expires_at``).
    """
    try:
        credentials = body["credentials"]
        username = credentials["username"]
        password = credentials["password"]
        expires_in = body["expires_in"]
        topic = body["topic"]
    except (KeyError, TypeError):
        raise TemporarilyUnavailable("malformed answer") from None
    if (
        not isinstance(username, str)
        or not isinstance(password, str)
        or not isinstance(topic, str)
        or not username
        or not password
        or not topic
        or isinstance(expires_in, bool)
        or not isinstance(expires_in, (int, float))
        or expires_in < MIN_EXPIRES_IN_SECONDS
    ):
        raise TemporarilyUnavailable("malformed answer")
    expires_in = min(float(expires_in), MAX_EXPIRES_IN_SECONDS)
    return MqttToken(username, password, expires_in, topic, received_at)


def _retry_after(response: aiohttp.ClientResponse) -> float | None:
    value = response.headers.get("Retry-After")
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        return None
    return seconds if seconds is not None and 0 < seconds < 86400 else None


async def async_mint_token(
    session: aiohttp.ClientSession,
    api_key: str,
    *,
    base_url: str = API_BASE_URL,
    clock=time.monotonic,
) -> MqttToken:
    """Exchange the API key for an MQTT token.

    Raises:
        InvalidAuth, NotBoatOwner, NoSubscription, RateLimited,
        TemporarilyUnavailable.
    """
    started = clock()
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT):
            async with session.post(
                base_url + TOKEN_PATH, headers={"X-API-Key": api_key}
            ) as response:
                status = response.status
                if status == 200:
                    try:
                        body = await response.json(content_type=None)
                    except (ValueError, aiohttp.ContentTypeError):
                        raise TemporarilyUnavailable("malformed answer") from None
                    return parse_token_response(body, started)
                retry_after = _retry_after(response)
    except (TimeoutError, aiohttp.ClientError):
        raise TemporarilyUnavailable("cannot reach the Smartboatia cloud") from None

    if status == 401:
        raise InvalidAuth("invalid API key")
    if status == 403:
        raise NotBoatOwner("key creator is no longer owner of the boat")
    if status == 402:
        raise NoSubscription("boat has no active subscription")
    if status == 429:
        raise RateLimited(retry_after)
    raise TemporarilyUnavailable(f"unexpected status {status}")
