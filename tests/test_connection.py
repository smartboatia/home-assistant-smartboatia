"""Token renewal and reconnect logic."""

from __future__ import annotations

import asyncio
import time

import pytest

from custom_components.smartboatia.api import (
    InvalidAuth,
    MqttToken,
    RateLimited,
    TemporarilyUnavailable,
)
from custom_components.smartboatia.connection import (
    STATE_AUTH_FAILED,
    STATE_CONNECTED,
    SmartboatiaConnection,
    backoff_delay,
    is_reusable,
    renew_delay,
)


def tok(received_at: float, expires_in: float, name: str = "x") -> MqttToken:
    return MqttToken(f"u-{name}", f"p-{name}", expires_in, "boat/n", received_at)


def test_renew_delay_uses_receive_time_plus_expires_in():
    token = tok(1000.0, 3600)
    assert renew_delay(token, 1000.0, 300) == 3300
    assert renew_delay(token, 4400.0, 300) == 0  # already due
    assert renew_delay(token, 5000.0, 300) == 0


def test_reuse():
    token = tok(0.0, 3600)
    assert is_reusable(token, 100.0, 60)
    assert not is_reusable(token, 3560.0, 60)


def test_backoff_grows_caps_and_has_jitter():
    mid = lambda: 0.5  # noqa: E731
    delays = [backoff_delay(n, 5, 900, mid) for n in range(1, 12)]
    assert delays[0] == 5
    assert delays == sorted(delays)
    assert delays[-1] == 900
    assert backoff_delay(1, 5, 900, lambda: 0.0) == 3.75
    assert backoff_delay(1, 5, 900, lambda: 1.0) == 6.25


class FakeLink:
    """Stands in for the paho link."""

    instances: list[FakeLink] = []

    def __init__(self, loop, token, on_payload, *, connect=True):
        self.token = token
        self.connected = asyncio.Event()
        self.lost = asyncio.Event()
        self.auth_rejected = False
        self.stopped = False
        self._connect = connect
        FakeLink.instances.append(self)

    def start(self):
        if self._connect:
            self.connected.set()

    async def stop(self):
        self.stopped = True


@pytest.fixture(autouse=True)
def _reset():
    FakeLink.instances = []


def make_conn(hass, mints, **kw):
    states = []
    auth = []
    queue = list(mints)

    async def mint():
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    conn = SmartboatiaConnection(
        hass,
        None,
        "key",
        on_payload=lambda p: None,
        on_state=states.append,
        on_auth_failed=lambda: auth.append(True),
        mint=mint,
        link_factory=FakeLink,
        clock=time.monotonic,
        backoff_min=0.01,
        **kw,
    )
    return conn, states, auth, queue


async def wait_for(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


async def test_renews_before_expiry_with_successor(hass):
    now = time.monotonic()
    first = tok(now, 0.6, "a")
    second = tok(now, 3600, "b")
    conn, states, _auth, _q = make_conn(
        hass, [second], first_token=first, renew_margin=0.4, reuse_margin=0.0
    )
    conn.async_start()
    await wait_for(lambda: len(FakeLink.instances) >= 2)
    assert FakeLink.instances[0].token is first
    assert FakeLink.instances[0].stopped
    assert FakeLink.instances[1].token is second
    assert conn.renewals == 1
    assert conn.mints == 1  # the first token came from the setup
    assert STATE_CONNECTED in states
    await conn.async_stop()
    assert FakeLink.instances[1].stopped


async def test_renew_failure_keeps_link_and_retries(hass):
    now = time.monotonic()
    first = tok(now, 1.0, "a")
    second = tok(now, 3600, "b")
    conn, _s, _a, _q = make_conn(
        hass,
        [TemporarilyUnavailable("x"), RateLimited(None), second],
        first_token=first,
        renew_margin=0.9,
        reuse_margin=0.0,
        renew_retry=0.05,
    )
    conn.async_start()
    await wait_for(lambda: conn.renewals == 1)
    # the first link stayed up until the successor existed
    assert not FakeLink.instances[0].stopped or len(FakeLink.instances) == 2
    assert conn.mints == 3
    await conn.async_stop()


async def test_lost_link_reuses_valid_token_without_minting(hass):
    first = tok(time.monotonic(), 3600, "a")
    conn, _s, _a, _q = make_conn(hass, [], first_token=first)
    conn.async_start()
    await wait_for(lambda: len(FakeLink.instances) == 1)
    FakeLink.instances[0].lost.set()
    await wait_for(lambda: len(FakeLink.instances) == 2)
    assert FakeLink.instances[1].token is first
    assert conn.mints == 0
    await conn.async_stop()


async def test_lost_link_after_expiry_mints_new_token(hass):
    first = tok(time.monotonic(), 0.3, "a")
    second = tok(time.monotonic(), 3600, "b")
    conn, _s, _a, _q = make_conn(
        hass, [second], first_token=first, renew_margin=0.0, reuse_margin=0.05
    )
    conn.async_start()
    await wait_for(lambda: len(FakeLink.instances) == 1)
    FakeLink.instances[0].lost.set()
    await asyncio.sleep(0.4)
    await wait_for(lambda: len(FakeLink.instances) >= 2)
    assert FakeLink.instances[-1].token is second
    await conn.async_stop()


async def test_auth_failure_at_mint_stops_and_reports(hass):
    conn, states, auth, _q = make_conn(hass, [InvalidAuth("x")])
    conn.async_start()
    await wait_for(lambda: auth)
    assert STATE_AUTH_FAILED in states
    assert FakeLink.instances == []
    await conn.async_stop()


async def test_auth_failure_at_renewal_reports(hass):
    first = tok(time.monotonic(), 0.5, "a")
    conn, _s, auth, _q = make_conn(
        hass, [InvalidAuth("x")], first_token=first, renew_margin=0.4, reuse_margin=0
    )
    conn.async_start()
    await wait_for(lambda: auth)
    await conn.async_stop()


async def test_broker_rejecting_credentials_drops_token(hass):
    first = tok(time.monotonic(), 3600, "a")
    second = tok(time.monotonic(), 3600, "b")

    created = []

    def factory(loop, token, on_payload):
        link = FakeLink(loop, token, on_payload, connect=False)
        if not created:
            link.auth_rejected = True
            link.lost.set()
        else:
            link._connect = True
        created.append(link)
        return link

    states = []
    conn = SmartboatiaConnection(
        hass,
        None,
        "key",
        on_payload=lambda p: None,
        on_state=states.append,
        on_auth_failed=lambda: None,
        first_token=first,
        mint=_async_returning(second),
        link_factory=factory,
        backoff_min=0.01,
    )
    conn.async_start()
    await wait_for(lambda: len(created) == 2)
    assert created[1].token is second
    assert conn.mints == 1
    await conn.async_stop()


def _async_returning(value):
    async def mint():
        return value

    return mint
