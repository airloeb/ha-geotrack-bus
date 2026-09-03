"""Config flow for GeoTrack Bus Tracking."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import GeoTrackApi, GeoTrackAuthError, GeoTrackConnectionError, normalize_cookie
from .const import (
    CONF_COOKIE,
    CONF_HOST,
    DEFAULT_HOST,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .coordinator import GeoTrackConfigEntry

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
        vol.Required(CONF_COOKIE): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
        ),
    }
)

STEP_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_COOKIE): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
        ),
    }
)


class GeoTrackConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for GeoTrack Bus Tracking."""

    VERSION = 1

    async def _async_validate(self, host: str, cookie: str) -> tuple[int, str | None]:
        """Return the number of visible buses, or an error key."""
        session = async_create_clientsession(hass=self.hass, cookie_jar=aiohttp.DummyCookieJar())
        api = GeoTrackApi(session, host, cookie)
        try:
            buses = await api.async_get_buses()
        except GeoTrackAuthError:
            return 0, "invalid_auth"
        except GeoTrackConnectionError:
            return 0, "cannot_connect"
        return len(buses), None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the portal host and a browser session cookie."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip().rstrip("/")
            host = host.removeprefix("https://").removeprefix("http://").split("/")[0]
            cookie = normalize_cookie(user_input[CONF_COOKIE])

            if not cookie:
                errors[CONF_COOKIE] = "invalid_auth"
            else:
                await self.async_set_unique_id(host)
                self._abort_if_unique_id_configured()
                _count, error = await self._async_validate(host, cookie)
                if error:
                    errors["base"] = error
                else:
                    return self.async_create_entry(
                        title=host,
                        data={CONF_HOST: host, CONF_COOKIE: cookie},
                        options={CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL},
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, user_input or {}
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start re-authentication after the cookie expires."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a freshly captured cookie."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            cookie = normalize_cookie(user_input[CONF_COOKIE])
            if not cookie:
                errors[CONF_COOKIE] = "invalid_auth"
            else:
                host = entry.data.get(CONF_HOST, DEFAULT_HOST)
                _count, error = await self._async_validate(host, cookie)
                if error:
                    errors["base"] = error
                else:
                    return self.async_update_reload_and_abort(
                        entry, data_updates={CONF_COOKIE: cookie}
                    )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"host": entry.data.get(CONF_HOST, DEFAULT_HOST)},
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: GeoTrackConfigEntry) -> GeoTrackOptionsFlow:
        """Return the options flow."""
        return GeoTrackOptionsFlow()


class GeoTrackOptionsFlow(OptionsFlow):
    """Handle the polling interval option."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            )

        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=current): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=MAX_SCAN_INTERVAL,
                        step=5,
                        unit_of_measurement="seconds",
                        mode=NumberSelectorMode.BOX,
                    )
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
