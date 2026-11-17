"""The paho link: callbacks without a network, plus the real client on a closed port."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import socket

from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from custom_components.smartboatia.api import MqttToken
from custom_components.smartboatia.connection import PahoLink

CONNACK_OK = ReasonCode(PacketTypes.CONNACK, "Success")
SUBACK_OK = ReasonCode(PacketTypes.SUBACK, "Granted QoS 0")
SUBACK_DENIED = ReasonCode(PacketTypes.SUBACK, "Unspecified error")


def make(hass):
    payloads = []
    token = MqttToken("api:u", "jwt", 3600.0, "boat/n", 0.0)
    link = PahoLink(hass.loop, token, lambda payload, retained: payloads.append((payload, retained)))
    return link, payloads


async def test_subscribes_only_to_the_granted_topic_and_is_ready_after_suback(hass):
    link, _ = make(hass)
    client = MagicMock()
    link._on_connect(client, None, {}, CONNACK_OK, None)
    await asyncio.sleep(0)
    client.subscribe.assert_called_once_with("boat/n", qos=0)
    assert not link.connected.is_set()  # not until the subscription is granted
    link._on_subscribe(client, None, 1, [SUBACK_OK], None)
    await asyncio.sleep(0)
    assert link.connected.is_set()
    assert not link.lost.is_set()
    client.publish.assert_not_called()  # read-only


async def test_denied_suback_is_a_failure(hass):
    link, _ = make(hass)
    link._on_subscribe(MagicMock(), None, 1, [SUBACK_DENIED], None)
    await asyncio.sleep(0)
    assert link.lost.is_set()
    assert not link.connected.is_set()


async def test_auth_reject_is_flagged(hass):
    link, _ = make(hass)
    link._on_connect(
        MagicMock(), None, {}, ReasonCode(PacketTypes.CONNACK, "Not authorized"), None
    )
    await asyncio.sleep(0)
    assert link.lost.is_set()
    assert link.auth_rejected
    assert not link.connected.is_set()


async def test_only_the_granted_topic_is_delivered(hass):
    link, payloads = make(hass)
    link._on_message(None, None, SimpleNamespace(topic="other/n", payload=b"x", retain=False))
    link._on_message(None, None, SimpleNamespace(topic="boat/n", payload=b"y", retain=False))
    await asyncio.sleep(0)
    assert payloads == [(b"y", False)]


async def test_retain_flag_is_passed_on(hass):
    link, payloads = make(hass)
    link._on_message(None, None, SimpleNamespace(topic="boat/n", payload=b"r", retain=True))
    link._on_message(None, None, SimpleNamespace(topic="boat/n", payload=b"l", retain=False))
    await asyncio.sleep(0)
    assert payloads == [(b"r", True), (b"l", False)]


async def test_disconnect_signals_lost(hass):
    link, _ = make(hass)
    link._on_disconnect(
        MagicMock(),
        None,
        None,
        ReasonCode(PacketTypes.DISCONNECT, "Unspecified error"),
        None,
    )
    await asyncio.sleep(0)
    assert link.lost.is_set()


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


async def test_real_client_against_closed_port_stops_cleanly(hass, socket_enabled):
    """The real paho client runs its thread, never connects, and stop() ends it."""
    base = "custom_components.smartboatia.connection."
    with (
        patch(base + "MQTT_HOST", "127.0.0.1"),
        patch(base + "MQTT_PORT", _closed_port()),
        patch(base + "MQTT_TLS", False),
    ):
        link, _ = make(hass)
        link.start()
        thread = link._client._thread
        assert thread is not None and thread.is_alive()
        await asyncio.sleep(0.3)
        assert not link.connected.is_set()
        async with asyncio.timeout(10):
            await link.stop()
    assert not thread.is_alive()
