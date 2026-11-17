"""Constants for the Smartboatia integration."""

from __future__ import annotations

import re

DOMAIN = "smartboatia"

CONF_API_KEY = "api_key"

# Smartboatia cloud. Defaults taken from the Smartboatia app and hub configuration
# (api2.smartboatia.com for DISPATCH, mqtt.smartboatia.com:8883 with TLS for the
# broker). Deliberately constants only, not user options.
API_BASE_URL = "https://api2.smartboatia.com"
TOKEN_PATH = "/dispatch/v2/mqtt/api_keys/token"
MQTT_HOST = "mqtt.smartboatia.com"
MQTT_PORT = 8883
MQTT_TLS = True
MQTT_KEEPALIVE = 60

# sbk_ followed by 43 base64url characters
API_KEY_PATTERN = re.compile(r"^sbk_[A-Za-z0-9_-]{43}$")

REQUEST_TIMEOUT = 15.0

# Token handling (mirrors the Hub: renew shortly before the broker kicks the client)
RENEW_MARGIN_SECONDS = 5 * 60
# a token this close to expiry is not reused for a reconnect, a new one is minted
REUSE_MARGIN_SECONDS = 60
RENEW_RETRY_SECONDS = 60
CONNECT_TIMEOUT_SECONDS = 30.0

# Backoff (seconds). The cloud allows 20 exchanges per key per hour.
BACKOFF_MIN_SECONDS = 5.0
BACKOFF_MAX_SECONDS = 15 * 60.0
MINT_BACKOFF_MIN_SECONDS = 30.0
RATE_LIMIT_MIN_WAIT_SECONDS = 10 * 60.0
NO_SUBSCRIPTION_WAIT_SECONDS = 15 * 60.0

# Boat data is replaced about every 60 s; without a message for this long the
# values are treated as stale.
STALE_AFTER_SECONDS = 10 * 60
STALE_CHECK_INTERVAL_SECONDS = 60
