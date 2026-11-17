"""Token renewal, reconnect and rate-limit logic."""

from __future__ import annotations

import asyncio
import heapq
import logging
import time

import pytest

from custom_components.smartboatia.api import (
    InvalidAuth,
    MqttToken,
    NoSubscription,
    RateLimited,
    TemporarilyUnavailable,
)
from custom_components.smartboatia.connection import (
    STATE_AUTH_FAILED,
    STATE_CONNECTED,
    STATE_UNAVAILABLE,
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


def test_renew_margin_is_capped_at_half_the_lifetime():
    short = tok(1000.0, 20)
    assert renew_delay(short, 1000.0, 300) == 10
    assert is_reusable(short, 1000.0, 60)  # margin is capped at a quarter: 5 s
    assert not is_reusable(short, 1016.0, 60)


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
    """Stands in for the paho link.

    mode: ok (connects, stays up), down (never answers), reject (credentials refused),
    deny (subscription denied), kick (connects, then dropped at once).
    """

    instances: list[FakeLink] = []

    def __init__(self, loop, token, on_payload, *, mode="ok"):
        self.token = token
        self.mode = mode
        self.connected = asyncio.Event()
        self.lost = asyncio.Event()
        self.auth_rejected = False
        self.stopped = False
        self.lost_after: float | None = None
        self.sleep = None
        FakeLink.instances.append(self)

    def start(self):
        if self.mode == "ok":
            self.connected.set()
            if self.lost_after is not None:
                asyncio.ensure_future(self._lose_later())
        elif self.mode == "reject":
            self.auth_rejected = True
            self.lost.set()
        elif self.mode == "deny":
            self.lost.set()
        elif self.mode == "kick":
            self.connected.set()
            asyncio.ensure_future(self._kick())

    async def _kick(self):
        for _ in range(5):
            await asyncio.sleep(0)
        self.lost.set()

    async def _lose_later(self):
        await self.sleep(self.lost_after)
        self.lost.set()

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

    kw.setdefault("min_mint_interval", 0.0)
    conn = SmartboatiaConnection(
        hass,
        None,
        "key",
        on_payload=lambda p: None,
        on_state=states.append,
        on_auth_failed=lambda: auth.append(True),
        mint=mint,
        link_factory=FakeLink,
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
    assert conn.mints == 1  # the first token was handed in
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
    modes = iter(["reject", "ok"])

    def factory(loop, token, on_payload):
        return FakeLink(loop, token, on_payload, mode=next(modes))

    states, queue = [], [second]

    async def mint():
        return queue.pop(0)

    conn = SmartboatiaConnection(
        hass,
        None,
        "key",
        on_payload=lambda p: None,
        on_state=states.append,
        on_auth_failed=lambda: None,
        first_token=first,
        mint=mint,
        link_factory=factory,
        backoff_min=0.01,
        min_mint_interval=0.0,
    )
    conn.async_start()
    await wait_for(lambda: len(FakeLink.instances) == 2)
    assert FakeLink.instances[1].token is second
    assert conn.mints == 1
    await conn.async_stop()


# --- a crash inside the loop must not kill it -------------------------------------


async def test_link_factory_error_is_survived_and_not_leaked(hass, caplog):
    first = tok(time.monotonic(), 3600, "a")
    calls = []

    def factory(loop, token, on_payload):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("secret-in-message sbk_leak")
        return FakeLink(loop, token, on_payload)

    states = []
    conn = SmartboatiaConnection(
        hass,
        None,
        "key",
        on_payload=lambda p: None,
        on_state=states.append,
        on_auth_failed=lambda: None,
        first_token=first,
        link_factory=factory,
        backoff_min=0.01,
        min_mint_interval=0.0,
    )
    with caplog.at_level(logging.DEBUG):
        conn.async_start()
        await wait_for(lambda: conn.state == STATE_CONNECTED)
    assert len(FakeLink.instances) == 1
    assert STATE_UNAVAILABLE in states
    assert not conn._task.done()
    assert "secret-in-message" not in caplog.text
    assert "RuntimeError" in caplog.text
    await conn.async_stop()


async def test_start_error_and_unexpected_mint_error_are_survived(hass):
    class Exploding(FakeLink):
        def start(self):
            raise OSError("boom")

    made = []

    def factory(loop, token, on_payload):
        link = Exploding(loop, token, on_payload) if not made else FakeLink(loop, token, on_payload)
        made.append(link)
        return link

    results = [ValueError("weird"), tok(time.monotonic(), 3600)]

    async def mint():
        item = results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    conn = SmartboatiaConnection(
        hass,
        None,
        "key",
        on_payload=lambda p: None,
        on_state=lambda s: None,
        on_auth_failed=lambda: None,
        mint=mint,
        link_factory=factory,
        backoff_min=0.01,
        min_mint_interval=0.0,
    )
    conn.async_start()
    await wait_for(lambda: len(made) == 2 and made[1].connected.is_set())
    assert not conn._task.done()
    await conn.async_stop()


async def test_stop_swallows_an_error_that_ended_the_task(hass):
    conn, _s, _a, _q = make_conn(hass, [])

    async def boom():
        raise RuntimeError("ended")

    conn._task = hass.loop.create_task(boom())
    await asyncio.sleep(0)
    await conn.async_stop()  # must not raise
    await conn.async_stop()  # and is idempotent


# --- simulated time: how many exchanges and connects per hour, worst case ---------


class Sim:
    """Virtual time: a sleeper waits until the driver advances the clock to it."""

    def __init__(self):
        self.now = 1000.0
        self._timers: list = []
        self._seq = 0

    def clock(self):
        return self.now

    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        self._seq += 1
        heapq.heappush(self._timers, (self.now + delay, self._seq, future))
        await future

    async def run_until(self, end):
        while self.now < end:
            for _ in range(20):  # let every task react to what just happened
                await asyncio.sleep(0)
            while self._timers and self._timers[0][2].done():
                heapq.heappop(self._timers)  # cancelled sleeper
            if self._timers:
                deadline, _, future = heapq.heappop(self._timers)
                self.now = max(self.now, deadline)
                future.set_result(None)


def worst_window(times, width=3600.0):
    return max(
        (sum(1 for t in times if start <= t < start + width) for start in times),
        default=0,
    )


async def simulate(hass, mint_mode, link_mode, hours=3, lost_after=None):
    sim = Sim()
    mint_times, connect_times = [], []

    async def mint():
        mint_times.append(sim.now)
        if mint_mode == "503":
            raise TemporarilyUnavailable("x")
        if mint_mode == "402":
            raise NoSubscription("x")
        if mint_mode == "429":
            raise RateLimited(None)
        return tok(sim.now, 30 if mint_mode == "short" else 3600)

    def factory(loop, token, on_payload):
        connect_times.append(sim.now)
        link = FakeLink(loop, token, on_payload, mode=link_mode)
        link.sleep = sim.sleep
        link.lost_after = lost_after
        return link

    conn = SmartboatiaConnection(
        hass,
        None,
        "key",
        on_payload=lambda p: None,
        on_state=lambda s: None,
        on_auth_failed=lambda: None,
        mint=mint,
        link_factory=factory,
        clock=sim.clock,
        sleep=sim.sleep,
    )
    conn.async_start()
    async with asyncio.timeout(30):
        await sim.run_until(1000 + hours * 3600)
    await conn.async_stop()
    return conn, mint_times, connect_times


@pytest.mark.parametrize(
    ("mint_mode", "link_mode"),
    [
        ("ok", "ok"),
        ("503", "ok"),
        ("402", "ok"),
        ("429", "ok"),
        ("short", "ok"),  # a server that hands out 30 s tokens
        ("ok", "down"),  # broker not reachable
        ("ok", "reject"),  # broker refuses the credentials every time
        ("ok", "deny"),  # SUBACK denied every time
        ("ok", "kick"),  # broker drops the link right after connecting
        ("short", "kick"),
        ("503", "down"),
    ],
)
async def test_worst_case_exchanges_per_hour_stay_below_the_limit(
    hass, mint_mode, link_mode
):
    _conn, mints, connects = await simulate(hass, mint_mode, link_mode)
    assert worst_window(mints) <= 15, (mint_mode, link_mode, mints)  # limit is 20
    assert worst_window(connects) <= 60, (mint_mode, link_mode)  # TLS connects/hour


async def test_normal_operation_is_about_one_exchange_per_hour(hass):
    _conn, mints, connects = await simulate(hass, "ok", "ok", hours=5)
    assert 4 <= len(mints) <= 6
    assert len(connects) == len(mints)  # one connect per token


async def test_kick_after_connect_keeps_escalating(hass):
    """A link that is dropped right after connecting must not reconnect fast."""
    conn, mints, connects = await simulate(hass, "ok", "kick")
    gaps = [b - a for a, b in zip(connects, connects[1:])]
    assert gaps[-1] > gaps[0] * 4
    assert len(mints) <= 3  # the token is reused, not minted per reconnect
    assert conn.diagnostics()["failures"] >= 5


async def test_denied_subscription_escalates(hass):
    conn, _mints, connects = await simulate(hass, "ok", "deny")
    gaps = [b - a for a, b in zip(connects, connects[1:])]
    assert gaps[-1] > gaps[0] * 4
    assert conn.diagnostics()["failures"] >= 5


async def test_stable_link_resets_the_backoff(hass):
    """A link that was up for a while and then lost reconnects quickly."""
    conn, _mints, connects = await simulate(hass, "ok", "ok", hours=1, lost_after=100)
    gaps = [b - a for a, b in zip(connects, connects[1:])]
    # about 100 s up plus about 5 s backoff every time, never escalating
    assert max(gaps) < 130
    assert len(connects) > 20
    assert conn.diagnostics()["failures"] <= 1
