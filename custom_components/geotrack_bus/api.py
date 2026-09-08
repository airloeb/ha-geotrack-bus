"""Thin async client for the GeoTrack parent portal."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import asin, cos, radians, sin, sqrt
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

VEHICLES_PATH = "/Map/GetVehicles"
LOGIN_PATH = "/Account/Login"
VERIFY_PATH = "/Account/SecondStepVerification"

# ETA tuning. The portal publishes no arrival time, so it is learned: on each
# run we note when the bus first reached every stop number, and once it reaches
# our stop the portal tells us when that happened. The difference is the lead
# time for that stop number. Nothing is assumed about how long a stop takes.
MAX_LEAD_SECONDS = 7200.0   # anything further out than 2h is a mis-paired run
MIN_LEAD_SECONDS = 1.0
LEAD_SAMPLE_WINDOW = 20     # runs kept per distance band
# The portal usually stops updating a bus a few minutes before it reaches the
# stop, and only sometimes says "was by your stop". When it does not, the
# closest the bus got is taken as the arrival -- but only if it got near enough
# that it plainly served the stop.
MAX_INFERRED_ARRIVAL_M = 800.0
EARTH_RADIUS_M = 6371000.0
# Lead times are learned against distance-to-stop, in bands this wide.
BAND_METERS = 400.0
MAX_LEARN_DISTANCE_M = 40000.0

COMMUNICATION_SMS = "SMS"
COMMUNICATION_CALL = "PhoneCall"

_TOKEN_RE = re.compile(
    r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', re.IGNORECASE
)
_NO_ACCOUNT_RE = re.compile(r"not linked to any account", re.IGNORECASE)
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)

# The portal is an ASP.NET MVC app, so timestamps arrive as "/Date(1788468579000)/",
# optionally with a trailing "+0000" style offset.
_DOTNET_DATE_RE = re.compile(r"^/Date\((-?\d+)(?:([+-])(\d{2})(\d{2}))?\)/$")

# Status strings the portal builds for each of your stops, e.g.
#   "bus 123, Route: ABC12P is before stop number 1, Your stop number is 9"
#   "bus 123, Route: ABC12P was by your stop at 4:20 PM, Your stop number is 9"
_BUS_IN_LINE_RE = re.compile(r"^\s*bus\s+(\S+?)\s*,", re.IGNORECASE)
_ROUTE_RE = re.compile(r"Route:\s*(\S+)")
_BEFORE_STOP_RE = re.compile(r"before stop number\s*(\d+)", re.IGNORECASE)
# "has passed stop number 4, 1 Example Rd at 11:36 AM" -- progress
# through the route. The time is when it cleared THAT stop, not ours.
_PASSED_STOP_RE = re.compile(
    r"has passed stop number\s*(\d+)\s*,\s*(.*?)\s+at\s+"
    r"([0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?\s*[AP]M)",
    re.IGNORECASE,
)
_AT_STOP_RE = re.compile(r"(is at|arriving at|approaching)\s+your stop", re.IGNORECASE)
_PASSED_RE = re.compile(r"was by your stop at\s*([0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?\s*[AP]M)", re.IGNORECASE)
_YOUR_STOP_RE = re.compile(r"Your stop number is\s*(\d+)", re.IGNORECASE)

STATUS_APPROACHING = "approaching"
STATUS_AT_STOP = "at_stop"
STATUS_PASSED = "passed"
STATUS_UNKNOWN = "unknown"


class GeoTrackError(Exception):
    """Base error for this integration."""


class GeoTrackConnectionError(GeoTrackError):
    """The portal could not be reached."""


class GeoTrackAuthError(GeoTrackError):
    """The stored session cookie is no longer valid."""


class GeoTrackLoginError(GeoTrackError):
    """A step of the phone/code login was rejected by the portal."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        """Record which step failed so the config flow can show the right error."""
        super().__init__(message or reason)
        self.reason = reason


class CookieStore:
    """A minimal cookie jar.

    The portal issues cookies whose names contain "/", which is not a valid
    RFC 6265 token, so http.cookies (and therefore aiohttp's own jar) drops
    them. Parsing Set-Cookie by hand is the only way to keep the session.
    """

    def __init__(self) -> None:
        """Start empty."""
        self._cookies: dict[str, str] = {}

    def update(self, response: aiohttp.ClientResponse) -> None:
        """Absorb every Set-Cookie header on a response."""
        for header in response.headers.getall("Set-Cookie", []):
            pair, _, attrs = header.partition(";")
            name, sep, value = pair.strip().partition("=")
            if not sep or not name:
                continue
            lowered = attrs.lower()
            if "max-age=0" in lowered.replace(" ", "") or not value:
                self._cookies.pop(name, None)
                continue
            self._cookies[name] = value

    @property
    def header(self) -> str:
        """The Cookie request header for everything collected so far."""
        return "; ".join(f"{k}={v}" for k, v in self._cookies.items())

    def __bool__(self) -> bool:
        """Whether any cookie has been collected."""
        return bool(self._cookies)


def parse_dotnet_date(value: Any) -> datetime | None:
    """Convert an ASP.NET "/Date(ms)/" string into an aware datetime."""
    if not isinstance(value, str):
        return None
    match = _DOTNET_DATE_RE.match(value.strip())
    if not match:
        return None
    millis = int(match.group(1))
    stamp = datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
    if match.group(2):
        offset = timedelta(hours=int(match.group(3)), minutes=int(match.group(4)))
        if match.group(2) == "-":
            offset = -offset
        stamp = stamp.replace(tzinfo=timezone(offset))
    return stamp


def normalize_cookie(raw: str) -> str:
    """Accept a bare cookie string or a whole pasted `Cookie: ...` header line."""
    cookie = raw.strip()
    if cookie.lower().startswith("cookie:"):
        cookie = cookie.split(":", 1)[1]
    return " ".join(cookie.split()).strip("; ")


@dataclass(slots=True)
class Stop:
    """One of your stops as reported against a bus (one per child/stop)."""

    bus_id: int
    stop_number: int | None
    stop_address: str | None
    latitude: float | None
    longitude: float | None
    student_id: Any
    message: str
    route: str | None
    current_stop_number: int | None
    stops_away: int | None
    passed_at: str | None
    status: str
    # Filled in by the coordinator, which is what watches the bus over time.
    serving_bus: str | None = None
    # Which vehicle record carried this Response, as opposed to which bus the
    # message names. The two disagree often enough that it matters, and it is
    # not yet settled which one is authoritative -- see carried_by/message_bus
    # on the ETA sensor.
    carried_by: str | None = None
    # False when distance had to be taken from the carrying vehicle because the
    # bus the message names is not published in the feed at all.
    distance_from_named_bus: bool = True
    last_stop_address: str | None = None
    distance_m: float | None = None
    eta_minutes: float | None = None
    eta_runs: int = 0
    warning_distance_m: float | None = None
    arrival_inferred: bool = False

    @property
    def key(self) -> str:
        """Identity of the physical stop, stable across runs.

        Deliberately NOT the stop number: the portal renumbers the same stop
        every run (the same coordinates have been served as stop 1 in the
        morning and stop 6 in the afternoon), and the vehicle changes too. The
        coordinates are the one thing that does not move, so they are what
        entities and learned history hang off.

        Rounded to ~11 m, which absorbs jitter without merging real stops.
        """
        if self.latitude is not None and self.longitude is not None:
            return f"{self.latitude:.4f},{self.longitude:.4f}"
        if self.student_id is not None:
            return f"student{self.student_id}"
        return "stop"

    @property
    def slug(self) -> str:
        """Filesystem/entity-id-safe form of the stop identity."""
        return self.key.replace(".", "_").replace(",", "_").replace("-", "m")

    @property
    def legacy_key(self) -> str:
        """The pre-4.0.0 identity, used only to migrate stored history."""
        if self.stop_number is not None:
            return f"stop{self.stop_number}"
        if self.student_id is not None:
            return f"student{self.student_id}"
        return "stop"

    @property
    def label(self) -> str:
        """Human name for the stop.

        Cosmetic only. The portal's stop number changes run to run, so this is
        not used for identity -- see `key`.
        """
        if self.stop_address:
            return self.stop_address
        return "My stop"

    @classmethod
    def from_json(cls, data: dict[str, Any], bus_number: str = "") -> Stop | None:
        """Build a stop from one entry of a vehicle's Responses array.

        The portal puts a line for *every* stop on the account into each
        Response's Message, and returns a Response for stops served by other
        vehicles too. So the line matching this Response's StopNumber has to be
        picked out, and a line naming a different bus means this Response is not
        really about this vehicle -- returning None drops it rather than
        inventing a phantom stop.
        """
        raw = (data.get("Message") or "").strip()
        stop_number = data.get("StopNumber")
        line = _select_message_line(raw, stop_number)
        if line is None:
            _LOGGER.debug(
                "Dropped a Response on bus %s: no line claims stop %s. Message was %r",
                bus_number or "?", stop_number, raw[:400],
            )
            return None

        # The portal broadcasts every stop line to every tracked vehicle, so a
        # Response naming a different bus is normal rather than bogus. Trust the
        # message about which bus serves the stop, and keep the Response:
        # entities are keyed by the stop's coordinates, so it lands in the right
        # place regardless of which vehicle record carried it.
        line_bus = _BUS_IN_LINE_RE.search(line)
        serving_bus = line_bus.group(1).strip() if line_bus else None

        route_match = _ROUTE_RE.search(line)
        before_match = _BEFORE_STOP_RE.search(line)
        passed_stop_match = _PASSED_STOP_RE.search(line)
        passed_match = _PASSED_RE.search(line)
        your_match = _YOUR_STOP_RE.search(line)

        if stop_number is None and your_match:
            stop_number = int(your_match.group(1))

        # The portal describes progress two ways: "is before stop number N"
        # and "has passed stop number N, <address> at <time>".
        current_stop_number: int | None = None
        last_stop_address: str | None = None
        if before_match:
            current_stop_number = int(before_match.group(1))
        elif passed_stop_match:
            current_stop_number = int(passed_stop_match.group(1))
            last_stop_address = passed_stop_match.group(2).strip() or None

        passed_at: str | None = None
        if passed_match:
            # "was by your stop at 8:55 AM" -- unambiguous arrival.
            status = STATUS_PASSED
            stops_away: int | None = None
            passed_at = passed_match.group(1).upper()
        elif (
            passed_stop_match
            and stop_number is not None
            and current_stop_number == stop_number
        ):
            # It has passed the stop that is ours, so that time is the arrival.
            status = STATUS_PASSED
            stops_away = None
            passed_at = passed_stop_match.group(3).upper()
        elif _AT_STOP_RE.search(line):
            status = STATUS_AT_STOP
            stops_away = 0
        elif current_stop_number is not None and stop_number is not None:
            status = STATUS_APPROACHING
            stops_away = max(stop_number - current_stop_number, 0)
        else:
            status = STATUS_UNKNOWN
            stops_away = None

        return cls(
            bus_id=int(data.get("BusID") or 0),
            stop_number=stop_number,
            stop_address=data.get("StopAddress") or None,
            latitude=_as_coord(data.get("StopLat")),
            longitude=_as_coord(data.get("StopLon")),
            student_id=data.get("StudentID"),
            message=line,
            route=route_match.group(1) if route_match else None,
            current_stop_number=current_stop_number,
            stops_away=stops_away,
            passed_at=passed_at,
            status=status,
            last_stop_address=last_stop_address,
            serving_bus=serving_bus,
        )


def _select_message_line(message: str, stop_number: Any) -> str | None:
    """Pick the line of a multi-stop message that describes this stop."""
    lines = [ln.strip() for ln in message.splitlines() if ln.strip()]
    if not lines:
        return None
    if stop_number is not None:
        for line in lines:
            match = _YOUR_STOP_RE.search(line)
            if match and int(match.group(1)) == int(stop_number):
                return line
    if len(lines) == 1:
        return lines[0]
    # Several lines and none claims this stop number: not safely attributable.
    return None


def haversine_meters(
    lat1: float | None, lon1: float | None, lat2: float | None, lon2: float | None
) -> float | None:
    """Great-circle distance in metres between two positions."""
    if None in (lat1, lon1, lat2, lon2):
        return None
    p1, p2 = radians(lat1), radians(lat2)
    dp, dl = p2 - p1, radians(lon2 - lon1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return round(2 * EARTH_RADIUS_M * asin(sqrt(a)), 1)


def parse_passed_at(passed_at: str, now: datetime) -> datetime | None:
    """Turn the portal's "8:55 AM" into a datetime on the right day."""
    text = passed_at.strip().upper().replace(" ", "")
    for fmt in ("%I:%M%p", "%I:%M:%S%p"):
        try:
            parsed = datetime.strptime(text, fmt).time()
        except ValueError:
            continue
        stamp = now.replace(
            hour=parsed.hour, minute=parsed.minute,
            second=parsed.second, microsecond=0,
        )
        # A time that looks far in the future belongs to yesterday's run.
        if stamp - now > timedelta(hours=12):
            stamp -= timedelta(days=1)
        return stamp
    return None


@dataclass(slots=True)
class ArrivalLearner:
    """Learns how long before arrival the bus is at a given distance.

    The portal gives no ETA, so one is measured. Through a run we note when the
    bus is seen in each 400 m band of distance from the stop. When it reaches
    the stop the portal states the time it did so, and the gap back to those
    sightings is that band's lead time. "The bus is 1.2 miles out" then means
    whatever 1.2 miles has really meant on this route.

    Distance rather than stop number, because a rider whose stop is number 1
    gets no useful signal from stop numbers at all: "before stop 1" is the only
    value that ever appears, however far away the bus is. Distance keeps
    resolving right up to the door.

    Lead times are kept per route code, so a morning route and an afternoon
    route never contaminate each other.
    """

    # route -> distance band -> one mean lead time per run, in seconds
    leads: dict[str, dict[int, deque[float]]] = field(default_factory=dict)
    run_route: str | None = None
    # band -> sighting times during the current, unfinished run
    seen: dict[int, list[datetime]] = field(default_factory=dict)
    # Closest the bus has come during this run, and when.
    closest_m: float | None = None
    closest_at: datetime | None = None
    # The portal's own timestamp for the last reading used, so a frozen feed is
    # not mistaken for the bus sitting still at that distance.
    last_bus_update: datetime | None = None
    last_arrival_inferred: bool = False

    ROUTE_UNKNOWN = "_"

    @staticmethod
    def _route_key(route: str | None) -> str:
        return route or ArrivalLearner.ROUTE_UNKNOWN

    @staticmethod
    def band(distance_m: float) -> int:
        """The distance band a reading falls in."""
        return int(distance_m // BAND_METERS)

    def observe(
        self,
        route: str | None,
        distance_m: float | None,
        now: datetime,
        bus_update: datetime | None = None,
    ) -> None:
        """Note how far out the bus is, restarting if the route changed."""
        if distance_m is None or distance_m > MAX_LEARN_DISTANCE_M:
            return
        key = self._route_key(route)
        if key != self.run_route:
            self.run_route = key
            self._reset_run()
        # A frozen feed repeats the same reading every poll. Recording those
        # would bury the run's real spread under whatever distance it stalled at.
        if bus_update is not None and bus_update == self.last_bus_update:
            return
        self.last_bus_update = bus_update

        stamp = bus_update or now
        self.seen.setdefault(self.band(distance_m), []).append(stamp)
        if self.closest_m is None or distance_m < self.closest_m:
            self.closest_m = distance_m
            self.closest_at = stamp

    def _reset_run(self) -> None:
        """Forget the in-progress run without touching what has been learned."""
        self.seen = {}
        self.closest_m = None
        self.closest_at = None
        self.last_bus_update = None

    def is_stale(self, now: datetime, max_age: timedelta) -> bool:
        """Whether the portal has stopped moving this bus on."""
        if self.last_bus_update is None:
            return False
        return (now - self.last_bus_update) > max_age

    def finalize_by_closest_approach(self) -> int:
        """Close a run the portal never reported an arrival for.

        The bus tracked in, got close, and the feed went quiet. Treat the
        closest approach as the arrival: it is a few minutes early compared with
        the portal's own wording, but it is the difference between learning a
        route every day and learning it roughly once a week.
        """
        if not self.seen or self.closest_at is None or self.closest_m is None:
            self._reset_run()
            return 0
        if self.closest_m > MAX_INFERRED_ARRIVAL_M:
            # Never got near the stop, so nothing can be concluded about it.
            self._reset_run()
            return 0
        route, at = self.run_route, self.closest_at
        kept = self.record_arrival(route, at)
        self.last_arrival_inferred = True
        return kept

    def record_arrival(self, route: str | None, arrived_at: datetime) -> int:
        """Turn the finished run into lead times. Returns how many bands kept."""
        if not self.seen:
            return 0
        table = self.leads.setdefault(self._route_key(route), {})
        kept = 0
        for band, times in self.seen.items():
            leads = [
                lead for t in times
                if MIN_LEAD_SECONDS
                <= (lead := (arrived_at - t).total_seconds())
                <= MAX_LEAD_SECONDS
            ]
            if not leads:
                continue
            # One figure per run per band, so a bus idling in one band for a
            # long stretch cannot outvote the bands either side of it.
            table.setdefault(band, deque(maxlen=LEAD_SAMPLE_WINDOW)).append(
                sum(leads) / len(leads)
            )
            kept += 1
        # Consume the run so a repeated "passed" poll cannot double-count it.
        self._reset_run()
        self.last_arrival_inferred = False
        return kept

    def _mean(self, table: dict[int, deque[float]], band: int) -> float | None:
        samples = table.get(band)
        if not samples:
            return None
        return sum(samples) / len(samples)

    def eta_seconds(
        self, route: str | None, distance_m: float | None
    ) -> float | None:
        """Estimated seconds to the stop, or None if this distance is unmeasured."""
        if distance_m is None:
            return None
        table = self.leads.get(self._route_key(route))
        if not table:
            return None

        band = self.band(distance_m)
        exact = self._mean(table, band)
        if exact is not None:
            return exact

        # Interpolate between the nearest measured bands either side. Never
        # extrapolate: beyond the measured range we genuinely do not know.
        lower = max((b for b in table if b < band), default=None)
        upper = min((b for b in table if b > band), default=None)
        if lower is None or upper is None:
            return None
        low_v, high_v = self._mean(table, lower), self._mean(table, upper)
        if low_v is None or high_v is None:
            return None
        return low_v + (high_v - low_v) * ((band - lower) / (upper - lower))

    def warning_distance_m(
        self, route: str | None, threshold_seconds: float
    ) -> float | None:
        """The distance at which the bus first falls inside the warning window."""
        table = self.leads.get(self._route_key(route))
        if not table:
            return None
        inside = [
            b for b in sorted(table, reverse=True)
            if (m := self._mean(table, b)) is not None and m <= threshold_seconds
        ]
        if not inside:
            return None
        return (inside[0] + 1) * BAND_METERS

    def runs_recorded(self, route: str | None) -> int:
        """How many runs have contributed, judged by the best-sampled band."""
        table = self.leads.get(self._route_key(route))
        if not table:
            return 0
        return max((len(v) for v in table.values()), default=0)

    def to_json(self) -> dict[str, dict[str, list[float]]]:
        """Serialise for Home Assistant's storage helper."""
        return {
            route: {str(band): list(vals) for band, vals in table.items() if vals}
            for route, table in self.leads.items()
            if table
        }

    @classmethod
    def from_json(cls, data: dict) -> ArrivalLearner:
        """Restore from storage, discarding anything malformed."""
        learner = cls()
        for route, table in (data or {}).items():
            restored: dict[int, deque[float]] = {}
            for band, vals in (table or {}).items():
                try:
                    number = int(band)
                except (TypeError, ValueError):
                    continue
                good = [
                    float(v) for v in vals
                    if isinstance(v, (int, float))
                    and MIN_LEAD_SECONDS <= float(v) <= MAX_LEAD_SECONDS
                ]
                if good:
                    restored[number] = deque(
                        good[-LEAD_SAMPLE_WINDOW:], maxlen=LEAD_SAMPLE_WINDOW
                    )
            if restored:
                learner.leads[route] = restored
        return learner


@dataclass(slots=True)
class Bus:
    """A tracked vehicle and the stops it serves for this account."""

    bus_id: int
    bus_number: str
    latitude: float | None
    longitude: float | None
    speed: float | None
    bearing: int | None
    address: str | None
    last_update: datetime | None
    point_type: str | None
    bus_code: str | None
    stops: list[Stop] = field(default_factory=list)

    @property
    def name(self) -> str:
        """Device name for this bus."""
        return f"Bus {self.bus_number}" if self.bus_number else f"Bus {self.bus_id}"

    @property
    def route(self) -> str | None:
        """Route code, which the portal only reports inside the stop messages."""
        for stop in self.stops:
            if stop.route:
                return stop.route
        return None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Bus:
        """Build a bus from one entry of the GetVehicles array."""
        bus = cls._shell(data)
        number = bus.bus_number
        seen: set[str] = set()
        for item in data.get("Responses") or []:
            if not isinstance(item, dict):
                continue
            stop = Stop.from_json(item, number)
            if stop is None:
                continue
            # A vehicle can repeat the same stop several times over; keep one.
            if stop.key in seen:
                continue
            seen.add(stop.key)
            stop.carried_by = number.strip() or None
            # Distance is only meaningful from the bus that serves the stop.
            # A Response carried by some other vehicle still tells us the
            # status and the arrival time, but its position is irrelevant, and
            # measuring from it would teach the estimator nonsense.
            if stop.serving_bus is None or stop.serving_bus == number.strip():
                stop.distance_m = haversine_meters(
                    bus.latitude, bus.longitude, stop.latitude, stop.longitude
                )
            bus.stops.append(stop)

        offered = len(data.get("Responses") or [])
        if offered != len(bus.stops):
            _LOGGER.debug(
                "Bus %s: portal offered %d Response(s), kept %d stop(s)",
                bus.bus_number or bus.bus_id, offered, len(bus.stops),
            )
        return bus

    @classmethod
    def _shell(cls, data: dict[str, Any]) -> Bus:
        """The vehicle record on its own, before stops are attributed."""
        return cls(
            bus_id=int(data.get("BusID") or 0),
            bus_number=str(data.get("BusNumber") or "").strip(),
            latitude=_as_coord(data.get("Latitude")),
            longitude=_as_coord(data.get("Longitude")),
            speed=_as_float(data.get("Speed")),
            bearing=_as_int(data.get("Bearing")),
            address=(data.get("CurrentAddress") or "").strip() or None,
            last_update=parse_dotnet_date(data.get("LastTimeUpdatedUTC")),
            point_type=data.get("PointType") or None,
            bus_code=data.get("BusCode") or None,
            stops=[],
        )


def _resolve_missing_distances(buses: list[Bus]) -> None:
    """Fall back to the carrying vehicle when the named bus is not published.

    Distance is normally measured from the bus the message names. On the
    morning runs, though, the portal names a bus it never publishes as a
    vehicle -- it relays the text through whichever vehicle it is tracking. In
    that case the carrying vehicle is the only position on offer, and since the
    portal only publishes this account's own buses, it is the right one to use.
    Refusing it left the morning routes unable to learn at all.
    """
    published = {
        bus.bus_number.strip().lower() for bus in buses if bus.bus_number
    }
    for bus in buses:
        for stop in bus.stops:
            if stop.distance_m is not None:
                continue
            named = (stop.serving_bus or "").strip().lower()
            if named and named in published:
                # The named bus is in the feed; its own record carries the
                # distance, so leave this copy without one.
                continue
            stop.distance_m = haversine_meters(
                bus.latitude, bus.longitude, stop.latitude, stop.longitude
            )
            stop.distance_from_named_bus = False
            if stop.distance_m is not None:
                _LOGGER.debug(
                    "Stop %s: message names bus %s, which is not published; "
                    "measuring from carrier %s instead",
                    stop.key, stop.serving_bus, bus.bus_number,
                )


def _as_coord(value: Any) -> float | None:
    """Coordinates of exactly 0 mean "no fix", not the Gulf of Guinea."""
    number = _as_float(value)
    if number is None or number == 0:
        return None
    return number


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class GeoTrackApi:
    """Fetches vehicle positions using a session cookie copied from a browser."""

    def __init__(self, session: aiohttp.ClientSession, host: str, cookie: str) -> None:
        """Initialise the client."""
        self._session = session
        self._host = host
        self._cookie = normalize_cookie(cookie)

    def update_cookie(self, cookie: str) -> None:
        """Swap in a freshly captured cookie after re-authentication."""
        self._cookie = normalize_cookie(cookie)

    async def async_get_buses(self) -> list[Bus]:
        """Return every bus currently visible to this account."""
        url = f"https://{self._host}{VEHICLES_PATH}"
        headers = {
            "Cookie": self._cookie,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://{self._host}/Home",
        }
        params = {"schoolToken": "", "firstTimeLoad": "true"}

        try:
            response = await self._session.get(
                url,
                params=params,
                headers=headers,
                allow_redirects=False,
                timeout=REQUEST_TIMEOUT,
            )
            # A logged-out session is answered with a redirect to /Account/Login.
            if response.status in (301, 302, 303, 307, 308, 401, 403):
                raise GeoTrackAuthError("Session cookie rejected by the portal")
            if response.status >= 400:
                raise GeoTrackConnectionError(f"Portal returned HTTP {response.status}")
            body = await response.text()
        except aiohttp.ClientError as err:
            raise GeoTrackConnectionError(f"Cannot reach {self._host}: {err}") from err
        except asyncio.TimeoutError as err:
            raise GeoTrackConnectionError(f"Timed out talking to {self._host}") from err

        try:
            payload = json.loads(body)
        except ValueError as err:
            # An HTML body here means the login page came back with a 200.
            raise GeoTrackAuthError("Portal returned HTML instead of JSON") from err

        if not isinstance(payload, list):
            raise GeoTrackAuthError("Unexpected payload from the portal")

        records = [item for item in payload if isinstance(item, dict)]
        buses = [Bus.from_json(item) for item in records]
        _resolve_missing_distances(buses)

        if _LOGGER.isEnabledFor(logging.DEBUG):
            summary = ", ".join(
                f"{bus.bus_number or bus.bus_id}"
                f"(offered={len(record.get('Responses') or [])},kept={len(bus.stops)})"
                for record, bus in zip(records, buses)
            )
            _LOGGER.debug(
                "Fetched %d bus(es) from %s: %s",
                len(buses), self._host, summary or "none",
            )
        return buses


class GeoTrackLogin:
    """Walks the portal's two-step phone/code login and yields a session cookie.

    The portal has no password: step one posts a phone number and it texts (or
    reads out) a short code, step two posts that code back. What comes out is an
    ordinary session cookie, the same thing you would copy from a browser.
    """

    def __init__(self, session: aiohttp.ClientSession, host: str) -> None:
        """Initialise the login helper."""
        self._session = session
        self._host = host
        self._cookies = CookieStore()

    def _url(self, path: str) -> str:
        return f"https://{self._host}{path}"

    def _headers(self, referer: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        if self._cookies:
            headers["Cookie"] = self._cookies.header
        if referer:
            headers["Referer"] = self._url(referer)
        return headers

    async def _get_token(self, path: str, referer: str | None = None) -> str:
        """Fetch a page and pull its ASP.NET anti-forgery token out of the form."""
        try:
            response = await self._session.get(
                self._url(path),
                headers=self._headers(referer),
                allow_redirects=False,
                timeout=REQUEST_TIMEOUT,
            )
            self._cookies.update(response)
            body = await response.text()
        except aiohttp.ClientError as err:
            raise GeoTrackConnectionError(f"Cannot reach {self._host}: {err}") from err
        except asyncio.TimeoutError as err:
            raise GeoTrackConnectionError(f"Timed out talking to {self._host}") from err

        match = _TOKEN_RE.search(body)
        if not match:
            raise GeoTrackLoginError(
                "unexpected_response", f"No login form at {path}"
            )
        return match.group(1)

    async def _post(self, path: str, data: dict[str, str], referer: str) -> tuple[int, str]:
        """Post a form and return (status, body), never following redirects."""
        try:
            response = await self._session.post(
                self._url(path),
                data=data,
                headers={
                    **self._headers(referer),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                allow_redirects=False,
                timeout=REQUEST_TIMEOUT,
            )
            self._cookies.update(response)
            return response.status, await response.text()
        except aiohttp.ClientError as err:
            raise GeoTrackConnectionError(f"Cannot reach {self._host}: {err}") from err
        except asyncio.TimeoutError as err:
            raise GeoTrackConnectionError(f"Timed out talking to {self._host}") from err

    async def async_request_code(
        self, phone: str, communication: str = COMMUNICATION_SMS
    ) -> None:
        """Ask the portal to send a login code to this phone number."""
        digits = re.sub(r"\D", "", phone)
        token = await self._get_token(LOGIN_PATH)
        status, body = await self._post(
            LOGIN_PATH,
            {
                "__RequestVerificationToken": token,
                "Phone": digits,
                "Communication": communication,
            },
            referer=LOGIN_PATH,
        )
        # Success is a redirect to the code page; a 200 means the form came back
        # with a validation message on it.
        if status in (301, 302, 303, 307, 308):
            _LOGGER.debug("Login code requested for %s", digits[-4:].rjust(len(digits), "*"))
            return
        if _NO_ACCOUNT_RE.search(body):
            raise GeoTrackLoginError("phone_not_found")
        raise GeoTrackLoginError("cannot_request_code")

    async def async_submit_code(self, phone: str, code: str) -> str:
        """Submit the code and return the resulting Cookie header."""
        digits = re.sub(r"\D", "", phone)
        clean_code = code.strip()
        verify_path = f"{VERIFY_PATH}?Phone={digits}"
        token = await self._get_token(verify_path, referer=LOGIN_PATH)
        status, _body = await self._post(
            VERIFY_PATH,
            {
                "__RequestVerificationToken": token,
                "Phone": digits,
                "Code": clean_code,
            },
            referer=verify_path,
        )
        if status not in (301, 302, 303, 307, 308):
            raise GeoTrackLoginError("invalid_code")
        if not self._cookies:
            raise GeoTrackLoginError("unexpected_response", "No session cookie issued")

        # Prove the session actually works before handing it to the coordinator,
        # rather than discovering it at the first poll.
        cookie = self._cookies.header
        await GeoTrackApi(self._session, self._host, cookie).async_get_buses()
        return cookie
