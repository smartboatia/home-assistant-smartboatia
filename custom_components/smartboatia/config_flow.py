"""Config flow for Smartboatia (API key, with reauthentication)."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    InvalidAuth,
    NoSubscription,
    NotBoatOwner,
    RateLimited,
    SmartboatiaError,
    TemporarilyUnavailable,
    async_mint_token,
)
from .const import API_KEY_PATTERN, CONF_API_KEY, DOMAIN
from .entity import short_boat_id

_LOGGER = logging.getLogger(__name__)

STEP_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="off")
        )
    }
)


class SmartboatiaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow."""

    VERSION = 1

    async def _validate(self, api_key: str) -> tuple[str | None, str | None]:
        """Try the key; return (boat id, error key)."""
        api_key = api_key.strip()
        if not API_KEY_PATTERN.match(api_key):
            return None, "invalid_key_format"
        try:
            token = await async_mint_token(async_get_clientsession(self.hass), api_key)
        except InvalidAuth:
            return None, "invalid_auth"
        except NotBoatOwner:
            return None, "not_owner"
        except NoSubscription:
            return None, "no_subscription"
        except RateLimited:
            return None, "rate_limited"
        except TemporarilyUnavailable:
            return None, "cannot_connect"
        except SmartboatiaError:
            return None, "unknown"
        return token.boat_id, None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the API key."""
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            boat_id, error = await self._validate(api_key)
            if error is None and boat_id is not None:
                await self.async_set_unique_id(boat_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Smartboatia {short_boat_id(boat_id)}",
                    data={CONF_API_KEY: api_key},
                )
            errors["base"] = error or "unknown"
        return self.async_show_form(
            step_id="user", data_schema=STEP_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new API key for the same boat."""
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            boat_id, error = await self._validate(api_key)
            if error is None:
                entry = self._get_reauth_entry()
                if boat_id != entry.unique_id:
                    return self.async_abort(reason="wrong_boat")
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_KEY: api_key}
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=STEP_SCHEMA, errors=errors
        )
