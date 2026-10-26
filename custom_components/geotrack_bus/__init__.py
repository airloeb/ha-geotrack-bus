"""The GeoTrack Bus Tracking integration."""

from __future__ import annotations

import aiohttp
from homeassistant.const import CONF_SCAN_INTERVAL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import GeoTrackApi
from .busright_api import BusRightApi, BusRightAuth
from .busright_coordinator import BusRightConfigEntry, BusRightCoordinator
from .const import (
    CONF_COOKIE,
    CONF_HOST,
    CONF_PROVIDER,
    CONF_REFRESH_TOKEN,
    DEFAULT_HOST,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PROVIDER_BUSRIGHT,
)
from .coordinator import GeoTrackConfigEntry, GeoTrackCoordinator

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.DEVICE_TRACKER,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: GeoTrackConfigEntry) -> bool:
    """Set up GeoTrack Bus Tracking from a config entry."""
    if entry.data.get(CONF_PROVIDER) == PROVIDER_BUSRIGHT:
        return await _async_setup_busright(hass, entry)

    # The portal's cookie names contain "/", which aiohttp's cookie jar rejects,
    # so send the cookie header by hand and keep the jar out of the way.
    session = async_create_clientsession(hass, cookie_jar=aiohttp.DummyCookieJar())

    api = GeoTrackApi(
        session,
        entry.data.get(CONF_HOST, DEFAULT_HOST),
        entry.data[CONF_COOKIE],
    )
    coordinator = GeoTrackCoordinator(
        hass,
        entry,
        api,
        entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
    )
    await coordinator.async_load_history()
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def _async_setup_busright(
    hass: HomeAssistant, entry: BusRightConfigEntry
) -> bool:
    """Set up an entry signed in to BusRight."""
    session = async_create_clientsession(hass)
    api = BusRightApi(session, BusRightAuth(session, entry.data[CONF_REFRESH_TOKEN]))
    coordinator = BusRightCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _remove_dropped_devices(hass, entry, coordinator)
    # The coordinator rewrites the entry's data whenever Firebase rotates the
    # refresh token; only a change of options should reload.
    options = dict(entry.options)

    async def _async_options_changed(
        hass: HomeAssistant, entry: BusRightConfigEntry
    ) -> None:
        if dict(entry.options) != options:
            await hass.config_entries.async_reload(entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_async_options_changed))
    return True


def _remove_dropped_devices(
    hass: HomeAssistant, entry: BusRightConfigEntry, coordinator: BusRightCoordinator
) -> None:
    """Remove devices for routes and stops no longer being followed."""
    wanted = {(DOMAIN, f"busright:{route_id}") for route_id in coordinator.statuses}
    wanted |= {
        (DOMAIN, f"busright_stop:{status.watched.stop_key}")
        for status in coordinator.statuses.values()
    }
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if not device.identifiers & wanted:
            registry.async_update_device(
                device.id, remove_config_entry_id=entry.entry_id
            )


async def async_unload_entry(hass: HomeAssistant, entry: GeoTrackConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: GeoTrackConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
