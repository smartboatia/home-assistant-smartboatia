"""The paho link, without a network: constructor and callbacks."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from paho.mqtt.reasoncodes import ReasonCode
from paho.mqtt.packettypes import PacketTypes

from custom_components.smartboatia.api import MqttToken
from custom_components.smartboatia.connection import PahoLink


def make(hass):
    payloads = []
    token = MqttToken("api:u", "jwt", 3600.0, "boat/n", 0.0)
    link = PahoLink(hass.loop, token, payloads.append)
    return link, payloads


async def test_connect_subscribes_only_to_the_granted_topic(hass):
    link, _ = make(hass)
    client = MagicMock()
    link._on_connect(client, None, {}, ReasonCode(PacketTypes.CONNACK, "Success"), None)
    await asyncio.sleep(0)
    client.subscribe.assert_called_once_with("boat/n", qos=0)
    assert link.connected.is_set()
    # read-only: never publishes
    client.publish.assert_not_called()


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
    link._on_message(None, None, SimpleNamespace(topic="other/n", payload=b"x"))
    link._on_message(None, None, SimpleNamespace(topic="boat/n", payload=b"y"))
    await asyncio.sleep(0)
    assert payloads == [b"y"]


async def test_disconnect_signals_lost(hass):
    link, _ = make(hass)
    link._on_disconnect(MagicMock(), None, None, ReasonCode(PacketTypes.DISCONNECT, "Unspecified error"), None)
    await asyncio.sleep(0)
    assert link.lost.is_set()
