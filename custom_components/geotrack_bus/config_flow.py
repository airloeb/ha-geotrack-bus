"""Config flow for GeoTrack Bus Tracking."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_REAUTH,
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
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    COMMUNICATION_CALL,
    COMMUNICATION_SMS,
    GeoTrackApi,
    GeoTrackAuthError,
    GeoTrackConnectionError,
    GeoTrackLogin,
    GeoTrackLoginError,
    normalize_cookie,
)
from .const import (
    CONF_COMMUNICATION,
    CONF_COOKIE,
    CONF_HOST,
    CONF_PHONE,
    DEFAULT_HOST,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .coordinator import GeoTrackConfigEntry

COOKIE_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
)

STEP_LOGIN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
        vol.Required(CONF_PHONE): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEL)
        ),
        vol.Required(CONF_COMMUNICATION, default=COMMUNICATION_SMS): SelectSelector(
            SelectSelectorConfig(
                options=[COMMUNICATION_SMS, COMMUNICATION_CALL],
                translation_key="communication",
                mode=SelectSelectorMode.LIST,
            )
        ),
    }
)

STEP_CODE_SCHEMA = vol.Schema({vol.Required("code"): str})

STEP_COOKIE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
        vol.Required(CONF_COOKIE): COOKIE_SELECTOR,
    }
)


def _clean_host(host: str) -> str:
    """Reduce whatever the user pasted to a bare hostname."""
    host = host.strip().rstrip("/")
    return host.removeprefix("https://").removeprefix("http://").split("/")[0]


class GeoTrackConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for GeoTrack Bus Tracking."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise per-flow state."""
        self._host: str = DEFAULT_HOST
        self._phone: str = ""
        self._communication: str = COMMUNICATION_SMS
        self._login: GeoTrackLogin | None = None

    @callback
    def _new_session(self) -> aiohttp.ClientSession:
        """A session with no cookie jar; this integration tracks cookies itself."""
        return async_create_clientsession(
            self.hass, cookie_jar=aiohttp.DummyCookieJar()
        )

    async def _async_finish(self, cookie: str) -> ConfigFlowResult:
        """Create the entry, or update it when re-authenticating."""
        data = {CONF_HOST: self._host, CONF_COOKIE: cookie}
        if self._phone:
            data[CONF_PHONE] = self._phone

        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates=data
            )

        await self.async_set_unique_id(self._host)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=self._host,
            data=data,
            options={CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick how to authenticate."""
        return self.async_show_menu(step_id="user", menu_options=["login", "cookie"])

    async def async_step_login(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the portal to send a login code to the parent's phone."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._host = _clean_host(user_input[CONF_HOST])
            self._phone = user_input[CONF_PHONE].strip()
            self._communication = user_input[CONF_COMMUNICATION]

            if self.source != SOURCE_REAUTH:
                await self.async_set_unique_id(self._host)
                self._abort_if_unique_id_configured()

            self._login = GeoTrackLogin(self._new_session(), self._host)
            try:
                await self._login.async_request_code(self._phone, self._communication)
            except GeoTrackLoginError as err:
                errors["base"] = err.reason
            except GeoTrackConnectionError:
                errors["base"] = "cannot_connect"
            else:
                return await self.async_step_code()

        return self.async_show_form(
            step_id="login",
            data_schema=self.add_suggested_values_to_schema(
                STEP_LOGIN_SCHEMA,
                user_input or {CONF_HOST: self._host, CONF_PHONE: self._phone},
            ),
            errors=errors,
        )

    async def async_step_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Exchange the texted code for a session cookie."""
        errors: dict[str, str] = {}

        if user_input is not None:
            assert self._login is not None
            try:
                cookie = await self._login.async_submit_code(
                    self._phone, user_input["code"]
                )
            except GeoTrackLoginError as err:
                errors["base"] = err.reason
            except GeoTrackAuthError:
                errors["base"] = "invalid_auth"
            except GeoTrackConnectionError:
                errors["base"] = "cannot_connect"
            else:
                return await self._async_finish(cookie)

        return self.async_show_form(
            step_id="code",
            data_schema=STEP_CODE_SCHEMA,
            errors=errors,
            description_placeholders={"phone": self._phone},
        )

    async def async_step_cookie(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Accept a session cookie copied out of a browser."""
        errors: dict[str, str] = {}
        schema: vol.Schema = STEP_COOKIE_SCHEMA

        if self.source == SOURCE_REAUTH:
            self._host = self._get_reauth_entry().data.get(CONF_HOST, DEFAULT_HOST)
            schema = vol.Schema({vol.Required(CONF_COOKIE): COOKIE_SELECTOR})

        if user_input is not None:
            if CONF_HOST in user_input:
                self._host = _clean_host(user_input[CONF_HOST])
            cookie = normalize_cookie(user_input[CONF_COOKIE])

            if not cookie:
                errors[CONF_COOKIE] = "invalid_auth"
            else:
                if self.source != SOURCE_REAUTH:
                    await self.async_set_unique_id(self._host)
                    self._abort_if_unique_id_configured()
                try:
                    await GeoTrackApi(
                        self._new_session(), self._host, cookie
                    ).async_get_buses()
                except GeoTrackAuthError:
                    errors["base"] = "invalid_auth"
                except GeoTrackConnectionError:
                    errors["base"] = "cannot_connect"
                else:
                    return await self._async_finish(cookie)

        return self.async_show_form(
            step_id="cookie",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start re-authentication after the session stops working."""
        self._host = entry_data.get(CONF_HOST, DEFAULT_HOST)
        self._phone = entry_data.get(CONF_PHONE, "")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the same two ways back in."""
        return self.async_show_menu(
            step_id="reauth_confirm", menu_options=["login", "cookie"]
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

        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
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
