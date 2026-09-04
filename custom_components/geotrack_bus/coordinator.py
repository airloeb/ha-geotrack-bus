"""Polling coordinator for GeoTrack Bus Tracking."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    STATUS_APPROACHING,
    Bus,
    GeoTrackApi,
    GeoTrackAuthError,
    GeoTrackConnectionError,
    MAX_STOP_SECONDS,
    MIN_STOP_SECONDS,
    ETA_SAMPLE_WINDOW,
    StopPace,
)
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.pace"
SAVE_DELAY = 300

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
        self._pace: dict[str, StopPace] = {}
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)

    async def async_load_pace(self) -> None:
        """Restore measured stop-to-stop times from a previous run."""
        stored = await self._store.async_load()
        if not stored:
            return
        for key, samples in stored.items():
            pace = StopPace()
            pace.samples.extend(
                float(s) for s in samples[-ETA_SAMPLE_WINDOW:]
                if MIN_STOP_SECONDS <= float(s) <= MAX_STOP_SECONDS
            )
            self._pace[key] = pace
        _LOGGER.debug("Restored pace data for %d stop(s)", len(self._pace))

    def _save_pace(self) -> None:
        """Persist measured times so an ETA survives a restart."""
        self._store.async_delay_save(
            lambda: {k: list(v.samples) for k, v in self._pace.items() if v.samples},
            SAVE_DELAY,
        )

    def _apply_eta(self, buses: list[Bus]) -> None:
        """Time each bus's progress and turn stops-away into minutes."""
        now = dt_util.utcnow()
        for bus in buses:
            for stop in bus.stops:
                key = f"{bus.bus_id}:{stop.key}"
                pace = self._pace.setdefault(key, StopPace())
                pace.observe(stop.current_stop_number, now)

                stop.seconds_per_stop = round(pace.seconds_per_stop, 1)
                stop.eta_samples = len(pace.samples)
                stop.eta_learned = pace.learned
                if stop.status == STATUS_APPROACHING and stop.stops_away is not None:
                    stop.eta_minutes = round(
                        stop.stops_away * pace.seconds_per_stop / 60, 1
                    )
        self._save_pace()

    async def _async_update_data(self) -> dict[int, Bus]:
        """Fetch the current position of every bus on the account."""
        try:
            buses = await self.api.async_get_buses()
        except GeoTrackAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except GeoTrackConnectionError as err:
            raise UpdateFailed(str(err)) from err

        self._apply_eta(buses)

        # Buses drop out of the feed between runs; keep the last known state so
        # entities go stale rather than disappearing entirely.
        merged = dict(self.data or {})
        merged.update({bus.bus_id: bus for bus in buses})
        return merged
