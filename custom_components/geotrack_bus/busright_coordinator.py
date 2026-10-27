"""Polling coordinator for BusRight routes.

BusRight says where the bus is and which stops the route makes, but not how far
along the route the bus has got -- the parent app has no "N stops away" to read.
So that is worked out here, run by run, from the live position:

* A stop counts as **reached** when the bus comes within ``REACH_M`` of it.
  Progress only moves forward, and only a few stops at a time, so a later stop
  that happens to sit beside an earlier one cannot make the bus leap ahead.
* Your stop is **passed** once the bus has reached it (or come close) and then
  pulled away again.
* Everything resets when BusRight starts a new run, which it marks with a new
  ``sessionId`` on the route.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    MAX_POSITION_AGE,
    STATUS_APPROACHING,
    STATUS_AT_STOP,
    STATUS_PASSED,
    STATUS_UNKNOWN,
    haversine_meters,
)
from .busright_api import (
    BusRightApi,
    BusRightAuthError,
    BusRightConnectionError,
    Position,
    Route,
)
from .const import (
    CONF_REFRESH_TOKEN,
    CONF_ROUTES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STATUS_NOT_RUNNING = "not_running"

# Route details (stop list, whether a run is on) change rarely; the position
# changes every few seconds. Fetch the route far less often.
ROUTE_REFRESH = timedelta(minutes=2)
# While no run is on there is nothing to watch, so the position is only looked
# at as often as the route itself.
IDLE_POSITION_REFRESH = ROUTE_REFRESH

# Within this distance the bus is taken to have reached a stop.
REACH_M = 100.0
# Within this distance of your stop, the bus is "at" it.
AT_STOP_M = 75.0
# After coming this close to your stop...
PASS_NEAR_M = 150.0
# ...the bus has passed once it is this much further away again.
PASS_LEAVE_M = 150.0
# Progress may skip stops nobody is waiting at, but not leap down the route.
MAX_SKIP = 3
# A stored stop is matched back to the route by id, or failing that by being
# this close to one of its stops (stop ids change when a route is re-planned).
STOP_MATCH_M = 150.0
# Two of your stops this close together are the same place on different runs
# (the morning pick-up and afternoon drop-off at one house), so they share
# one set of stop entities.
SAME_STOP_M = 75.0
# Below this the bus is parked or idling rather than driving.
MOVING_M = 15.0
# A fix this far from the last one, faster than a bus can drive, is not the
# bus: the first live run saw the position flick to a fixed point 2 km away
# every few reads. Such a jump is only believed once it repeats in a row.
MAX_SPEED_MPS = 35.0
MIN_JUMP_M = 300.0
JUMP_CONFIRMATIONS = 3
JUMP_AGREE_M = 150.0
# Floor for the ETA: even at 30 mph in a straight line the bus needs this long
# to cover the distance, whatever the plan says.
FLOOR_SPEED_MPS = 13.4
# Snapshots of what BusRight sent during runs, for diagnostics. Only changes
# are kept, so this covers several runs.
CAPTURE_MAX = 600

type BusRightConfigEntry = ConfigEntry[BusRightCoordinator]


@dataclass(frozen=True)
class WatchedRoute:
    """A route from the entry's options, with the stop you care about."""

    district_id: str
    school_id: str
    route_id: str
    name: str
    stop_id: str | None
    stop_latitude: float
    stop_longitude: float
    stop_label: str
    stop_key: str

    @classmethod
    def from_options(cls, raw: dict[str, Any], stop_key: str) -> WatchedRoute:
        """Build from one entry of the ``routes`` option."""
        return cls(
            district_id=raw["district_id"],
            school_id=raw["school_id"],
            route_id=raw["route_id"],
            name=raw.get("name") or raw["route_id"],
            stop_id=raw.get("stop_id"),
            stop_latitude=float(raw["stop_latitude"]),
            stop_longitude=float(raw["stop_longitude"]),
            stop_label=raw.get("stop_label") or "My stop",
            stop_key=stop_key,
        )


def assign_stop_keys(
    routes: list[dict[str, Any]], existing: list[dict[str, Any]] | None = None
) -> list[str]:
    """Give each route's stop a key, shared by stops at the same place.

    The key names the stop's entities, so it must not change when routes are
    added or dropped later. A key already stored on a route is kept, and a new
    stop near one in ``existing`` takes that stop's key.
    """
    keys: list[str] = []
    placed: list[tuple[str, float, float]] = [
        (raw["stop_key"], float(raw["stop_latitude"]), float(raw["stop_longitude"]))
        for raw in existing or []
        if raw.get("stop_key")
    ]
    for raw in routes:
        lat, lng = float(raw["stop_latitude"]), float(raw["stop_longitude"])
        if raw.get("stop_key"):
            key = raw["stop_key"]
        else:
            key = next(
                (
                    pkey
                    for pkey, plat, plng in placed
                    if (haversine_meters(lat, lng, plat, plng) or SAME_STOP_M + 1)
                    <= SAME_STOP_M
                ),
                f"{lat:.4f}_{lng:.4f}",
            )
        placed.append((key, lat, lng))
        keys.append(key)
    return keys


@dataclass
class RunState:
    """What has happened so far on the current run of one route."""

    session_id: str | None = None
    reached_index: int = -1
    reached_at: datetime | None = None
    closest_m: float | None = None
    passed_at: datetime | None = None
    arrived: bool = False
    # Fixes too far from the last believed one, waiting to be confirmed.
    jumps: list[Position] = field(default_factory=list)


@dataclass
class RouteStatus:
    """One route and where its bus stands relative to your stop."""

    watched: WatchedRoute
    route: Route | None = None
    position: Position | None = None
    position_stale: bool = True
    moving: bool | None = None
    my_index: int | None = None
    status: str = STATUS_UNKNOWN
    distance_m: float | None = None
    stops_away: int | None = None
    eta_minutes: float | None = None
    last_reached: str | None = None
    passed_at: datetime | None = None
    run: RunState = field(default_factory=RunState)

    @property
    def running(self) -> bool:
        """Whether BusRight reports a run in progress."""
        return bool(self.route and self.route.active)

    @property
    def stop_count(self) -> int:
        """Stops on the route."""
        return len(self.route.stops) if self.route else 0


def match_stop(route: Route, watched: WatchedRoute) -> int | None:
    """Find your stop on the route as it stands today."""
    if watched.stop_id:
        for stop in route.stops:
            if stop.stop_id == watched.stop_id:
                return stop.index
    best: tuple[float, int] | None = None
    for stop in route.stops:
        distance = haversine_meters(
            stop.latitude, stop.longitude,
            watched.stop_latitude, watched.stop_longitude,
        )
        if distance is not None and distance <= STOP_MATCH_M:
            if best is None or distance < best[0]:
                best = (distance, stop.index)
    return best[1] if best else None


def advance(status: RouteStatus, now: datetime) -> None:
    """Fold a fresh position into the run and recompute what it means.

    Expects ``status.route``, ``status.position`` and ``status.my_index`` to be
    current, and ``status.position_stale`` to be false.
    """
    route, position, run = status.route, status.position, status.run
    assert route is not None and position is not None

    # Advance through the stops in order. Only look a few ahead, so a stop
    # further down the route that passes near an earlier one is not mistaken
    # for progress. Until the first stop of a run is found, look at every stop
    # up to yours, since watching may have started mid-run after a restart --
    # but never beyond it, or one bad fix near a later stop would declare the
    # bus past you before it had set off.
    start = run.reached_index + 1
    if run.reached_index >= 0:
        end = start + MAX_SKIP
    elif status.my_index is not None:
        end = status.my_index + 1
    else:
        end = len(route.stops)
    window = route.stops[start:end]
    hits = [
        (position.distance_to(stop.latitude, stop.longitude), stop.index)
        for stop in window
    ]
    hits = [(d, i) for d, i in hits if d is not None and d <= REACH_M]
    if hits:
        run.reached_index = min(hits)[1]
        run.reached_at = now

    if status.my_index is None:
        return
    mine = route.stops[status.my_index]
    distance = position.distance_to(mine.latitude, mine.longitude)
    status.distance_m = distance
    if distance is None:
        return
    if run.closest_m is None or distance < run.closest_m:
        run.closest_m = distance
    if distance <= AT_STOP_M or run.reached_index >= status.my_index:
        run.arrived = True

    if run.passed_at is None:
        came_close = run.arrived or (
            run.closest_m is not None and run.closest_m <= PASS_NEAR_M
        )
        if came_close and distance > (run.closest_m or 0) + PASS_LEAVE_M:
            run.passed_at = now


def describe(status: RouteStatus, now: datetime) -> None:
    """Set the status fields entities read, from the run so far."""
    route, run = status.route, status.run
    status.passed_at = run.passed_at
    status.stops_away = None
    status.eta_minutes = None
    status.last_reached = None

    if route is None:
        status.status = STATUS_UNKNOWN
        return
    if 0 <= run.reached_index < len(route.stops):
        status.last_reached = route.stops[run.reached_index].label

    if run.passed_at is not None:
        status.status = STATUS_PASSED
        return
    if not route.active:
        status.status = STATUS_NOT_RUNNING
        status.distance_m = None if status.position_stale else status.distance_m
        return
    if status.my_index is None or status.position_stale:
        # A run is on but either your stop is not on the route any more or the
        # tablet has stopped reporting; say nothing rather than guess.
        status.status = STATUS_UNKNOWN
        if status.position_stale:
            status.distance_m = None
        return

    if status.distance_m is not None and status.distance_m <= AT_STOP_M:
        status.status = STATUS_AT_STOP
    else:
        status.status = STATUS_APPROACHING

    status.stops_away = max(0, status.my_index - run.reached_index - 1)

    # BusRight's planner times each leg of the route. From the last stop the
    # bus reached, the planned time left to yours, less the time since, is
    # a fair estimate -- and the only one available until a stop is reached.
    mine = route.stops[status.my_index]
    if run.reached_index >= 0 and run.reached_at is not None:
        last = route.stops[run.reached_index]
        if mine.scheduled_offset is not None and last.scheduled_offset is not None:
            remaining = (
                mine.scheduled_offset
                - last.scheduled_offset
                - (now - run.reached_at).total_seconds()
            )
            if status.distance_m is not None:
                remaining = max(remaining, status.distance_m / FLOOR_SPEED_MPS)
            status.eta_minutes = round(max(0.0, remaining) / 60, 1)


class BusRightCoordinator(DataUpdateCoordinator[dict[str, RouteStatus]]):
    """Polls every watched route and keeps each run's progress."""

    config_entry: BusRightConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: BusRightConfigEntry,
        api: BusRightApi,
    ) -> None:
        """Initialise the coordinator."""
        options = config_entry.options or {}
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}_busright",
            update_interval=timedelta(
                seconds=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.api = api
        raw_routes = list(options.get(CONF_ROUTES) or [])
        # The flows store each stop's key; this only fills in any missing.
        keys = assign_stop_keys(raw_routes)
        self.statuses: dict[str, RouteStatus] = {}
        for raw, key in zip(raw_routes, keys):
            watched = WatchedRoute.from_options(raw, key)
            self.statuses[watched.route_id] = RouteStatus(watched=watched)
        self._route_fetched: dict[str, datetime] = {}
        self._position_fetched: dict[str, datetime] = {}
        self._last_candidates: dict[str, frozenset[tuple[float, float]]] = {}
        # Per route: coordinate pairs in the position document that are fixed
        # places, not the bus.
        self.fixed_points: dict[str, set[tuple[float, float]]] = {}
        self.capture: deque[dict[str, Any]] = deque(maxlen=CAPTURE_MAX)
        self._last_capture: dict[str, dict[str, Any]] = {}

    def _capture(self, status: RouteStatus, now: datetime) -> None:
        """Record what BusRight sent while a run is on, when it has changed.

        BusRight's app shows its own ETA, which this integration does not yet
        know where to find. Keeping every distinct position document and the
        route's run-time fields alongside what was worked out here shows both
        where it lives and how far the two estimates differ.
        """
        route, position = status.route, status.position
        if route is None or not (route.active or not status.position_stale):
            return
        seen = {
            "route": route.name,
            "position_updated": (
                position.updated.isoformat() if position and position.updated else None
            ),
            "position_document": position.raw if position else None,
            "route_fields": route.extra,
            "stop_fields": route.stop_extra,
        }
        if self._last_capture.get(route.route_id) == seen:
            return
        self._last_capture[route.route_id] = seen
        self.capture.append(
            {
                "time": now.isoformat(),
                **seen,
                "worked_out": {
                    "status": status.status,
                    "eta_minutes": status.eta_minutes,
                    "stops_away": status.stops_away,
                    "distance_m": status.distance_m,
                    "last_stop_reached": status.last_reached,
                },
            }
        )

    async def _async_refresh_route(self, status: RouteStatus, now: datetime) -> None:
        """Re-read the route when it is due, starting a new run if one began."""
        watched = status.watched
        fetched = self._route_fetched.get(watched.route_id)
        if status.route is not None and fetched and now - fetched < ROUTE_REFRESH:
            return
        route = await self.api.async_get_route(
            watched.district_id, watched.school_id, watched.route_id
        )
        self._route_fetched[watched.route_id] = now
        was_active = status.route.active if status.route else False
        status.route = route
        status.my_index = match_stop(route, watched)
        if status.my_index is None:
            _LOGGER.warning(
                "Your stop (%s) is no longer on BusRight route %s; reconfigure "
                "the stop under Configure", watched.stop_label, route.name,
            )
        if route.session_id and route.session_id != status.run.session_id:
            if status.run.session_id is not None:
                _LOGGER.debug("New run on route %s", route.name)
            status.run = RunState(session_id=route.session_id)
        elif route.active and not was_active and not route.session_id:
            # No session id to go by; a run starting is the next best marker.
            status.run = RunState()

    async def _async_refresh_position(self, status: RouteStatus, now: datetime) -> None:
        """Read the live position, polling hard only while a run is on."""
        route = status.route
        if route is None or not route.db_path:
            return
        fetched = self._position_fetched.get(route.route_id)
        if (
            not route.active
            and fetched
            and now - fetched < IDLE_POSITION_REFRESH
        ):
            return
        previous = status.position
        near = (
            (previous.latitude, previous.longitude)
            if previous and not status.position_stale
            else None
        )
        position = await self.api.async_get_position(
            route.db_path, near, self.fixed_points.get(route.route_id, set())
        )
        self._position_fetched[route.route_id] = now
        if position is not None:
            self._learn_fixed(status, position)
            if near is not None and not self._believable(status, position, now):
                return

        status.position = position or previous
        age = status.position.age if status.position else None
        status.position_stale = age is None or age > MAX_POSITION_AGE
        if position is None or previous is None or status.position_stale:
            status.moving = None if status.position_stale else status.moving
            return
        moved = haversine_meters(
            previous.latitude, previous.longitude,
            position.latitude, position.longitude,
        )
        status.moving = moved is not None and moved > MOVING_M

    def _learn_fixed(self, status: RouteStatus, position: Position) -> None:
        """Note coordinate pairs that stay put while another one moves.

        The bus is the pair that changes from read to read. A pair repeated
        exactly while another changed is a fixed place riding along in the
        document, and is ignored from then on.
        """
        previous = self._last_candidates.get(status.watched.route_id)
        self._last_candidates[status.watched.route_id] = position.candidates
        if previous is None or len(position.candidates) < 2:
            return
        unchanged = position.candidates & previous
        if unchanged and position.candidates - previous:
            fixed = self.fixed_points.setdefault(status.watched.route_id, set())
            if not unchanged <= fixed:
                _LOGGER.debug(
                    "Treating %s as a fixed place on %s, not the bus",
                    sorted(unchanged - fixed), status.watched.name,
                )
                fixed |= unchanged

    def _believable(self, status: RouteStatus, position: Position, now: datetime) -> bool:
        """Whether a fix could be the same bus as the last believed one.

        A jump further than the bus could have driven is held back until a run
        of fixes bears it out -- each within driving distance of the one before
        -- which a real change of position (a gap in reporting, a restart) does
        and a stray point flicking in and out does not.
        """
        previous, run = status.position, status.run
        assert previous is not None

        def plausible(a: Position, b: Position) -> bool:
            gap = haversine_meters(a.latitude, a.longitude, b.latitude, b.longitude)
            seconds = (
                (b.updated - a.updated).total_seconds()
                if a.updated and b.updated
                else (now - (a.updated or now)).total_seconds()
            )
            return gap is None or gap <= max(MIN_JUMP_M, MAX_SPEED_MPS * abs(seconds))

        if plausible(previous, position):
            run.jumps.clear()
            return True
        if run.jumps and not plausible(run.jumps[-1], position):
            run.jumps.clear()
        run.jumps.append(position)
        if len(run.jumps) >= JUMP_CONFIRMATIONS:
            run.jumps.clear()
            return True
        _LOGGER.debug(
            "Ignoring a BusRight fix on %s that is further than the bus could "
            "have driven since the last one", status.watched.name,
        )
        return False

    async def _async_update_data(self) -> dict[str, RouteStatus]:
        """Refresh every route and work out where each bus has got to."""
        now = dt_util.utcnow()
        try:
            for status in self.statuses.values():
                await self._async_refresh_route(status, now)
                await self._async_refresh_position(status, now)
                if status.route and status.route.active and not status.position_stale:
                    advance(status, now)
                elif status.position_stale:
                    status.distance_m = None
                describe(status, now)
                self._capture(status, now)
        except BusRightAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except BusRightConnectionError as err:
            raise UpdateFailed(str(err)) from err

        self._persist_refresh_token()
        return self.statuses

    def _persist_refresh_token(self) -> None:
        """Store the refresh token if Firebase rotated it."""
        token = self.api.auth.refresh_token
        entry = self.config_entry
        if token and entry.data.get(CONF_REFRESH_TOKEN) != token:
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_REFRESH_TOKEN: token}
            )
