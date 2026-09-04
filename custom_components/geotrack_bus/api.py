"""Thin async client for the GeoTrack parent portal."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
LEAD_SAMPLE_WINDOW = 20     # runs kept per stop number

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
_ROUTE_RE = re.compile(r"Route:\s*(\S+)")
_BEFORE_STOP_RE = re.compile(r"before stop number\s*(\d+)", re.IGNORECASE)
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
    eta_minutes: float | None = None
    eta_runs: int = 0
    warning_stop_number: int | None = None

    @property
    def key(self) -> str:
        """Stable per-stop identifier used for entity unique ids."""
        if self.stop_number is not None:
            return f"stop{self.stop_number}"
        if self.student_id is not None:
            return f"student{self.student_id}"
        return "stop"

    @property
    def label(self) -> str:
        """Human name for the stop, used in entity names."""
        if self.stop_number is not None:
            return f"Stop {self.stop_number}"
        return "Stop"

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Stop:
        """Build a stop from one entry of a vehicle's Responses array."""
        message = (data.get("Message") or "").strip()
        route_match = _ROUTE_RE.search(message)
        before_match = _BEFORE_STOP_RE.search(message)
        passed_match = _PASSED_RE.search(message)
        your_match = _YOUR_STOP_RE.search(message)

        stop_number = data.get("StopNumber")
        if stop_number is None and your_match:
            stop_number = int(your_match.group(1))

        current_stop_number = int(before_match.group(1)) if before_match else None

        if passed_match:
            status = STATUS_PASSED
            stops_away: int | None = None
        elif _AT_STOP_RE.search(message):
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
            message=message,
            route=route_match.group(1) if route_match else None,
            current_stop_number=current_stop_number,
            stops_away=stops_away,
            passed_at=passed_match.group(1).upper() if passed_match else None,
            status=status,
        )


def parse_passed_at(passed_at: str, now: datetime) -> datetime | None:
    """Turn the portal's "4:20 PM" into a datetime on the right day."""
    text = passed_at.strip().upper().replace(" ", "")
    for fmt in ("%I:%M%p", "%I:%M:%S%p"):
        try:
            parsed = datetime.strptime(text, fmt).time()
        except ValueError:
            continue
        stamp = now.replace(
            hour=parsed.hour, minute=parsed.minute,
            second=getattr(parsed, "second", 0), microsecond=0,
        )
        # A time that looks far in the future belongs to yesterday's run.
        if stamp - now > timedelta(hours=12):
            stamp -= timedelta(days=1)
        return stamp
    return None


@dataclass(slots=True)
class ArrivalLearner:
    """Learns how far ahead of arrival the bus passes each stop number.

    The portal gives no ETA, so we measure one. During a run we note the first
    moment the bus is reported working each stop number. When it finally reaches
    our stop the portal states the time it did so, and the gap back to each of
    those moments is that stop number's lead time. Averaged over runs, "the bus
    is at stop 8" becomes "about six minutes away" — with no assumption that
    stops are evenly spaced or evenly slow.

    Lead times are kept per route code, so a morning route and an afternoon
    route never contaminate each other.
    """

    # route -> stop number -> recent lead times, in seconds
    leads: dict[str, dict[int, deque[float]]] = field(default_factory=dict)
    run_route: str | None = None
    first_seen: dict[int, datetime] = field(default_factory=dict)

    ROUTE_UNKNOWN = "_"

    @staticmethod
    def _route_key(route: str | None) -> str:
        return route or ArrivalLearner.ROUTE_UNKNOWN

    def observe(self, route: str | None, current_stop: int | None, now: datetime) -> None:
        """Note where the bus is, starting a fresh run if the route changed."""
        if current_stop is None:
            return
        key = self._route_key(route)
        if key != self.run_route:
            self.run_route = key
            self.first_seen = {}
        # Only the first sighting at a stop number counts; later polls at the
        # same stop would understate the lead.
        self.first_seen.setdefault(current_stop, now)

    def record_arrival(self, route: str | None, arrived_at: datetime) -> int:
        """Convert the finished run into lead times. Returns how many were kept."""
        key = self._route_key(route)
        if not self.first_seen:
            return 0
        table = self.leads.setdefault(key, {})
        kept = 0
        for stop_number, seen in self.first_seen.items():
            lead = (arrived_at - seen).total_seconds()
            if MIN_LEAD_SECONDS <= lead <= MAX_LEAD_SECONDS:
                table.setdefault(stop_number, deque(maxlen=LEAD_SAMPLE_WINDOW)).append(lead)
                kept += 1
        # Consume the run so a repeated "passed" poll cannot double-count it.
        self.first_seen = {}
        return kept

    def _mean(self, route_table: dict[int, deque[float]], stop_number: int) -> float | None:
        samples = route_table.get(stop_number)
        if not samples:
            return None
        return sum(samples) / len(samples)

    def eta_seconds(self, route: str | None, current_stop: int | None) -> float | None:
        """Estimated seconds to our stop, or None if this stop is unmeasured."""
        if current_stop is None:
            return None
        table = self.leads.get(self._route_key(route))
        if not table:
            return None

        exact = self._mean(table, current_stop)
        if exact is not None:
            return exact

        # Interpolate between the nearest measured stop numbers either side.
        # Never extrapolate: outside the measured range we simply do not know.
        lower = max((s for s in table if s < current_stop), default=None)
        upper = min((s for s in table if s > current_stop), default=None)
        if lower is None or upper is None:
            return None
        low_v, high_v = self._mean(table, lower), self._mean(table, upper)
        if low_v is None or high_v is None:
            return None
        span = upper - lower
        return low_v + (high_v - low_v) * ((current_stop - lower) / span)

    def warning_stop(self, route: str | None, threshold_seconds: float) -> int | None:
        """The stop number at which the bus first falls inside the warning window."""
        table = self.leads.get(self._route_key(route))
        if not table:
            return None
        candidates = [
            s for s in sorted(table)
            if (m := self._mean(table, s)) is not None and m <= threshold_seconds
        ]
        return candidates[0] if candidates else None

    def runs_recorded(self, route: str | None) -> int:
        """How many runs have contributed, judged by the best-sampled stop."""
        table = self.leads.get(self._route_key(route))
        if not table:
            return 0
        return max((len(v) for v in table.values()), default=0)

    def to_json(self) -> dict[str, dict[str, list[float]]]:
        """Serialise for Home Assistant's storage helper."""
        return {
            route: {str(stop): list(vals) for stop, vals in table.items() if vals}
            for route, table in self.leads.items()
            if table
        }

    @classmethod
    def from_json(cls, data: dict) -> ArrivalLearner:
        """Restore from storage, discarding anything malformed."""
        learner = cls()
        for route, table in (data or {}).items():
            restored: dict[int, deque[float]] = {}
            for stop, vals in (table or {}).items():
                try:
                    number = int(stop)
                except (TypeError, ValueError):
                    continue
                good = [
                    float(v) for v in vals
                    if isinstance(v, (int, float))
                    and MIN_LEAD_SECONDS <= float(v) <= MAX_LEAD_SECONDS
                ]
                if good:
                    restored[number] = deque(good[-LEAD_SAMPLE_WINDOW:],
                                             maxlen=LEAD_SAMPLE_WINDOW)
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
            stops=[Stop.from_json(item) for item in (data.get("Responses") or [])],
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

        buses = [Bus.from_json(item) for item in payload if isinstance(item, dict)]
        _LOGGER.debug("Fetched %d bus(es) from %s", len(buses), self._host)
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
