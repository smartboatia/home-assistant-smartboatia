"""Shared fixtures."""

from __future__ import annotations

import cbor2
import pytest

pytest_plugins = "pytest_homeassistant_custom_component"

EXAMPLE_TS = 1761578411000
# section 9 of the N-message specification (95 bytes)
EXAMPLE_HEX = (
    "ad0003011b0000019a264163f802a2000101040aa200fb404ac66a6e32e38201fb4023fca643cc07ab0"
    "bf94a4d0dfa3ecccccd0ea200a200f94a260118f001a101010ff9481a10fa447d4ccd12f9466613a200"
    "18f501a1020014f94c601506"
)
API_KEY = "sbk_" + "A" * 43
BOAT_ID = "boat-test-id"


def make_message(*items: tuple[int, object], origin: object = None) -> bytes:
    """Build an N-message v3 from (key, python value) pairs."""
    pairs = [(0, 3), (1, EXAMPLE_TS), (2, {0: 0} if origin is None else origin), *items]
    return bytes([0xA0 + len(pairs)]) + b"".join(
        cbor2.dumps(k) + (v if isinstance(v, bytes) else cbor2.dumps(v))
        for k, v in pairs
    )


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow the custom integration in every test."""
    return enable_custom_integrations
