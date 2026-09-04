"""The GeoTrack Bus Tracking integration."""

from __future__ import annotations

import aiohttp
from homeassistant.const import CONF_SCAN_INTERVAL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import GeoTrackApi
from .const import CONF_COOKIE, CONF_HOST, DEFAULT_HOST, DEFAULT_SCAN_INTERVAL
from .coordinator import GeoTrackConfigEntry, GeoTrackCoordinator

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.DEVICE_TRACKER,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: GeoTrackConfigEntry) -> bool:
    """Set up GeoTrack Bus Tracking from a config entry."""
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
    await coordinator.async_load_pace()
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GeoTrackConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: GeoTrackConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
