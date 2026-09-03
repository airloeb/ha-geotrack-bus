"""Polling coordinator for GeoTrack Bus Tracking."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import Bus, GeoTrackApi, GeoTrackAuthError, GeoTrackConnectionError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

type GeoTrackConfigEntry = ConfigEntry[GeoTrackCoordinator]


class GeoTrackCoordinator(DataUpdateCoordinator[dict[int, Bus]]):
    """Keeps one poll of /Map/GetVehicles shared across every entity."""

    config_entry: GeoTrackConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: GeoTrackConfigEntry,
        api: GeoTrackApi,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.api = api

    async def _async_update_data(self) -> dict[int, Bus]:
        """Fetch the current position of every bus on the account."""
        try:
            buses = await self.api.async_get_buses()
        except GeoTrackAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except GeoTrackConnectionError as err:
            raise UpdateFailed(str(err)) from err

        # Buses drop out of the feed between runs; keep the last known state so
        # entities go unavailable-ish (stale) rather than disappearing entirely.
        merged = dict(self.data or {})
        merged.update({bus.bus_id: bus for bus in buses})
        return merged
