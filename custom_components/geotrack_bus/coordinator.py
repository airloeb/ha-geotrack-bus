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
    STATUS_PASSED,
    ArrivalLearner,
    Bus,
    GeoTrackApi,
    GeoTrackAuthError,
    GeoTrackConnectionError,
    Stop,
    parse_passed_at,
)
from .const import (
    CONF_WARNING_MINUTES,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WARNING_MINUTES,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
# Keyed by distance band since 3.0.0. The pre-3.0.0 file used the same shape
# with stop numbers as keys, so it must not be read back as distances.
STORAGE_KEY = f"{DOMAIN}.arrivals_by_distance"
SAVE_DELAY = 300
# A bus absent from the feed this long is no longer worth showing.
STALE_AFTER = timedelta(hours=3)

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
        self._learners: dict[str, ArrivalLearner] = {}
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)

    async def async_load_history(self) -> None:
        """Restore measured lead times from previous runs."""
        stored = await self._store.async_load()
        if not stored:
            return
        for key, payload in stored.items():
            self._learners[key] = ArrivalLearner.from_json(payload)
        _LOGGER.debug("Restored arrival history for %d stop(s)", len(self._learners))

    def _learner_for(self, bus: Bus, stop: Stop) -> ArrivalLearner:
        """The learner for this physical stop, adopting old history if present.

        History used to be filed under "<bus id>:stop<number>", both parts of
        which change between runs -- the afternoon is a different vehicle and
        the portal renumbers the stop. It is now filed under the stop's
        coordinates.

        Legacy entries are adopted by matching the route code they hold rather
        than the bus id, since the route is what actually corresponds to a
        given approach. Only an unambiguous single match is taken, so two stops
        sharing a route are never silently merged.
        """
        key = stop.key
        if key in self._learners:
            return self._learners[key]

        legacy = [k for k in self._learners if ":" in k]
        if legacy and stop.route:
            matches = [
                k for k in legacy
                if stop.route in self._learners[k].leads
            ]
            if len(matches) == 1:
                self._learners[key] = self._learners.pop(matches[0])
                _LOGGER.info(
                    "Adopted learned arrival history from %s into stop %s "
                    "(matched on route %s)",
                    matches[0], key, stop.route,
                )
                return self._learners[key]
            if len(matches) > 1:
                _LOGGER.warning(
                    "Not migrating arrival history for stop %s: route %s "
                    "matches several legacy records (%s)",
                    key, stop.route, ", ".join(sorted(matches)),
                )

        self._learners[key] = ArrivalLearner()
        return self._learners[key]

    def _save_history(self) -> None:
        """Persist measured lead times so the estimate survives a restart."""
        self._store.async_delay_save(
            lambda: {k: v.to_json() for k, v in self._learners.items() if v.leads},
            SAVE_DELAY,
        )

    def _apply_eta(self, buses: list[Bus]) -> None:
        """Learn from this poll, then express stops-away as minutes."""
        now = dt_util.now()
        threshold = float(
            self.config_entry.options.get(
                CONF_WARNING_MINUTES, DEFAULT_WARNING_MINUTES
            )
        ) * 60

        for bus in buses:
            for stop in bus.stops:
                learner = self._learner_for(bus, stop)
                route = stop.route

                if stop.status == STATUS_APPROACHING:
                    learner.observe(route, stop.distance_m, now)
                elif stop.status == STATUS_PASSED and stop.passed_at:
                    # The portal states when the bus reached our stop, which is
                    # a better arrival time than anything we could infer from
                    # poll timing. Closing the run turns it into lead times.
                    arrived = parse_passed_at(stop.passed_at, now)
                    if arrived is not None:
                        kept = learner.record_arrival(route, arrived)
                        if kept:
                            _LOGGER.debug(
                                "Learned %d distance band(s) for bus %s stop %s on route %s",
                                kept, bus.bus_number, stop.stop_number, route,
                            )

                stop.eta_runs = learner.runs_recorded(route)
                stop.warning_distance_m = learner.warning_distance_m(route, threshold)
                if stop.status == STATUS_APPROACHING:
                    seconds = learner.eta_seconds(route, stop.distance_m)
                    stop.eta_minutes = (
                        round(seconds / 60, 1) if seconds is not None else None
                    )

        self._save_history()

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
        # a position stays visible rather than vanishing. But drop anything that
        # has gone quiet for hours, or a morning bus would still be sitting in
        # the data at bedtime claiming it had just passed the stop.
        merged = dict(self.data or {})
        merged.update({bus.bus_id: bus for bus in buses})

        current = {bus.bus_id for bus in buses}
        cutoff = dt_util.utcnow() - STALE_AFTER
        for bus_id, bus in list(merged.items()):
            if bus_id in current:
                continue
            if bus.last_update is None or bus.last_update < cutoff:
                del merged[bus_id]
        return merged
