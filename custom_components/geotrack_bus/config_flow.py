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
from .busright_api import (
    BusRightApi,
    BusRightAuth,
    BusRightAuthError,
    BusRightConnectionError,
    BusRightLogin,
    BusRightLoginError,
    Route,
    RouteRef,
)
from .const import (
    CONF_COMMUNICATION,
    CONF_EMAIL,
    CONF_PROVIDER,
    CONF_REFRESH_TOKEN,
    CONF_ROUTES,
    PROVIDER_BUSRIGHT,
    CONF_WARNING_MILES,
    CONF_WARNING_MINUTES,
    CONF_WARNING_STOPS,
    CONF_COOKIE,
    CONF_HOST,
    CONF_PHONE,
    DEFAULT_HOST,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WARNING_MILES,
    DEFAULT_WARNING_MINUTES,
    DEFAULT_WARNING_STOPS,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MAX_WARNING_MINUTES,
    MIN_SCAN_INTERVAL,
    MIN_WARNING_MINUTES,
)
from .busright_coordinator import assign_stop_keys
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


STEP_BUSRIGHT_EMAIL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL)
        )
    }
)


def _is_busright(entry: GeoTrackConfigEntry) -> bool:
    """Whether an entry is signed in to BusRight rather than GeoTrack."""
    return entry.data.get(CONF_PROVIDER) == PROVIDER_BUSRIGHT


class BusRightRoutePicker:
    """Steps for choosing BusRight routes and your stop on each.

    Shared by the config flow, after sign-in, and the options flow, so the
    choice can be changed later without signing in again.
    """

    _busright_api: BusRightApi | None
    _busright_refs: list[RouteRef]
    _busright_chosen: list[RouteRef]
    _busright_picked: list[dict[str, Any]]
    _busright_route: Route | None

    def _busright_reset(self) -> None:
        self._busright_refs = []
        self._busright_chosen = []
        self._busright_picked = []
        self._busright_route = None

    async def _async_busright_finish(self) -> ConfigFlowResult:
        """Called with ``_busright_picked`` filled in."""
        raise NotImplementedError

    def _busright_existing(self) -> list[dict[str, Any]]:
        """Routes already configured, whose stop keys new picks should reuse."""
        return []

    async def async_step_busright_routes(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose which routes to follow."""
        errors: dict[str, str] = {}
        assert self._busright_api is not None
        if not self._busright_refs:
            try:
                self._busright_refs = await self._busright_api.async_discover_routes()
            except BusRightAuthError:
                return self.async_abort(reason="invalid_auth")
            except BusRightConnectionError:
                return self.async_abort(reason="cannot_connect")
            if not self._busright_refs:
                return self.async_abort(reason="no_routes")

        if user_input is not None:
            wanted = set(user_input[CONF_ROUTES])
            self._busright_chosen = [
                ref for ref in self._busright_refs if ref.route_id in wanted
            ]
            if self._busright_chosen:
                self._busright_picked = []
                return await self.async_step_busright_stop()
            errors["base"] = "no_route_selected"

        options = [
            {
                "value": ref.route_id,
                "label": f"{ref.name}  ✓ your child rides this" if ref.yours else ref.name,
            }
            for ref in self._busright_refs
        ]
        default = [ref.route_id for ref in self._busright_refs if ref.yours]
        schema = vol.Schema(
            {
                vol.Required(CONF_ROUTES, default=default): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="busright_routes", data_schema=schema, errors=errors
        )

    async def async_step_busright_stop(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick your stop on the next chosen route."""
        assert self._busright_api is not None
        ref = self._busright_chosen[len(self._busright_picked)]

        if self._busright_route is None or self._busright_route.route_id != ref.route_id:
            try:
                self._busright_route = await self._busright_api.async_get_route(
                    ref.district_id, ref.school_id, ref.route_id
                )
            except BusRightAuthError:
                return self.async_abort(reason="invalid_auth")
            except BusRightConnectionError:
                return self.async_abort(reason="cannot_connect")
        route = self._busright_route
        if not route.stops:
            return self.async_abort(reason="no_stops")

        if user_input is not None:
            stop = route.stops[int(user_input["stop"])]
            self._busright_picked.append(
                {
                    "district_id": ref.district_id,
                    "school_id": ref.school_id,
                    "route_id": ref.route_id,
                    "name": route.name,
                    "stop_id": stop.stop_id,
                    "stop_latitude": stop.latitude,
                    "stop_longitude": stop.longitude,
                    "stop_label": stop.label,
                }
            )
            if len(self._busright_picked) < len(self._busright_chosen):
                return await self.async_step_busright_stop()
            keys = assign_stop_keys(self._busright_picked, self._busright_existing())
            for picked, key in zip(self._busright_picked, keys):
                picked["stop_key"] = key
            return await self._async_busright_finish()

        default = next(
            (str(s.index) for s in route.stops if s.stop_id and s.stop_id in ref.stop_ids),
            None,
        )
        options = [
            {"value": str(s.index), "label": f"{s.index + 1}. {s.label}"}
            for s in route.stops
        ]
        field = (
            vol.Required("stop", default=default) if default else vol.Required("stop")
        )
        schema = vol.Schema(
            {
                field: SelectSelector(
                    SelectSelectorConfig(
                        options=options, mode=SelectSelectorMode.DROPDOWN
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="busright_stop",
            data_schema=schema,
            description_placeholders={
                "route": route.name,
                "hint": (
                    "BusRight already lists your child at the stop chosen below."
                    if default
                    else "BusRight has not linked your child to this route, so "
                    "pick the stop yourself."
                ),
            },
        )


def _clean_host(host: str) -> str:
    """Reduce whatever the user pasted to a bare hostname."""
    host = host.strip().rstrip("/")
    return host.removeprefix("https://").removeprefix("http://").split("/")[0]


class GeoTrackConfigFlow(BusRightRoutePicker, ConfigFlow, domain=DOMAIN):
    """Handle a config flow for GeoTrack Bus Tracking."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise per-flow state."""
        self._host: str = DEFAULT_HOST
        self._phone: str = ""
        self._communication: str = COMMUNICATION_SMS
        self._login: GeoTrackLogin | None = None
        self._email: str = ""
        self._busright_login: BusRightLogin | None = None
        self._busright_auth: BusRightAuth | None = None
        self._busright_api = None
        self._busright_reset()

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
        return self.async_show_menu(
            step_id="user", menu_options=["login", "cookie", "busright"]
        )

    async def async_step_busright(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask BusRight to email a sign-in code."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip().lower()
            if self.source != SOURCE_REAUTH:
                await self.async_set_unique_id(f"busright:{self._email}")
                self._abort_if_unique_id_configured()

            self._busright_login = BusRightLogin(async_create_clientsession(self.hass))
            try:
                await self._busright_login.async_request_code(self._email)
            except BusRightLoginError as err:
                errors["base"] = err.reason
            except BusRightConnectionError:
                errors["base"] = "cannot_connect_busright"
            else:
                return await self.async_step_busright_code()

        return self.async_show_form(
            step_id="busright",
            data_schema=self.add_suggested_values_to_schema(
                STEP_BUSRIGHT_EMAIL_SCHEMA, user_input or {CONF_EMAIL: self._email}
            ),
            errors=errors,
        )

    async def async_step_busright_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Trade the emailed code for a sign-in, then choose routes."""
        errors: dict[str, str] = {}

        if user_input is not None:
            assert self._busright_login is not None
            try:
                auth = await self._busright_login.async_sign_in(
                    self._email, user_input["code"]
                )
            except BusRightLoginError as err:
                errors["base"] = err.reason
            except BusRightConnectionError:
                errors["base"] = "cannot_connect_busright"
            else:
                self._busright_auth = auth
                if self.source == SOURCE_REAUTH:
                    entry = self._get_reauth_entry()
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={CONF_REFRESH_TOKEN: auth.refresh_token},
                    )
                self._busright_api = BusRightApi(
                    async_create_clientsession(self.hass), auth
                )
                return await self.async_step_busright_routes()

        return self.async_show_form(
            step_id="busright_code",
            data_schema=STEP_CODE_SCHEMA,
            errors=errors,
            description_placeholders={"email": self._email},
        )

    async def _async_busright_finish(self) -> ConfigFlowResult:
        """Create the BusRight entry."""
        assert self._busright_auth is not None
        return self.async_create_entry(
            title=f"BusRight ({self._email})",
            data={
                CONF_PROVIDER: PROVIDER_BUSRIGHT,
                CONF_EMAIL: self._email,
                CONF_REFRESH_TOKEN: self._busright_auth.refresh_token,
            },
            options={
                CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                CONF_ROUTES: self._busright_picked,
            },
        )

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
        if entry_data.get(CONF_PROVIDER) == PROVIDER_BUSRIGHT:
            self._email = entry_data.get(CONF_EMAIL, "")
            return await self.async_step_busright()
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


class GeoTrackOptionsFlow(BusRightRoutePicker, OptionsFlow):
    """Handle the polling interval option."""

    def __init__(self) -> None:
        """Initialise per-flow state."""
        self._busright_api = None
        self._busright_reset()

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if _is_busright(self.config_entry):
            return await self.async_step_init_busright()
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_WARNING_MINUTES: int(user_input[CONF_WARNING_MINUTES]),
                    CONF_WARNING_MILES: float(user_input[CONF_WARNING_MILES]),
                    CONF_WARNING_STOPS: int(user_input[CONF_WARNING_STOPS]),
                }
            )

        # An entry created before these options existed has no options at all,
        # and `.get` on that raises, which surfaces as a 500 from the flow.
        options = self.config_entry.options or {}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=MAX_SCAN_INTERVAL,
                        step=5,
                        unit_of_measurement="seconds",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_WARNING_MINUTES,
                    default=options.get(
                        CONF_WARNING_MINUTES, DEFAULT_WARNING_MINUTES
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_WARNING_MINUTES,
                        max=MAX_WARNING_MINUTES,
                        step=1,
                        unit_of_measurement="minutes",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_WARNING_MILES,
                    default=options.get(CONF_WARNING_MILES, DEFAULT_WARNING_MILES),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0.1, max=10, step=0.05,
                        unit_of_measurement="miles",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_WARNING_STOPS,
                    default=options.get(CONF_WARNING_STOPS, DEFAULT_WARNING_STOPS),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=10, step=1,
                        unit_of_measurement="stops",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_init_busright(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer a BusRight entry's two kinds of option."""
        return self.async_show_menu(
            step_id="init_busright",
            menu_options=["busright_settings", "busright_routes"],
        )

    async def async_step_busright_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Polling and warning thresholds for a BusRight entry."""
        options = self.config_entry.options or {}
        if user_input is not None:
            return self.async_create_entry(
                data={
                    **options,
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_WARNING_MILES: float(user_input[CONF_WARNING_MILES]),
                    CONF_WARNING_STOPS: int(user_input[CONF_WARNING_STOPS]),
                }
            )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=MAX_SCAN_INTERVAL,
                        step=5,
                        unit_of_measurement="seconds",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_WARNING_MILES,
                    default=options.get(CONF_WARNING_MILES, DEFAULT_WARNING_MILES),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0.1, max=10, step=0.05,
                        unit_of_measurement="miles",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_WARNING_STOPS,
                    default=options.get(CONF_WARNING_STOPS, DEFAULT_WARNING_STOPS),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=10, step=1,
                        unit_of_measurement="stops",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="busright_settings", data_schema=schema)

    async def async_step_busright_routes(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-pick routes, reusing the running entry's sign-in."""
        if self._busright_api is None:
            coordinator = getattr(self.config_entry, "runtime_data", None)
            if coordinator is None:
                return self.async_abort(reason="not_loaded")
            self._busright_api = coordinator.api
        return await super().async_step_busright_routes(user_input)

    def _busright_existing(self) -> list[dict[str, Any]]:
        """Keep your stops' entity names when the routes change."""
        return list((self.config_entry.options or {}).get(CONF_ROUTES) or [])

    async def _async_busright_finish(self) -> ConfigFlowResult:
        """Save the new route choice."""
        return self.async_create_entry(
            data={**(self.config_entry.options or {}), CONF_ROUTES: self._busright_picked}
        )
