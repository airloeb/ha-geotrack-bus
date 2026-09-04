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

# ETA tuning. The portal publishes no arrival time, so minutes are derived from
# how long this bus actually takes between stops.
DEFAULT_SECONDS_PER_STOP = 70.0
MIN_STOP_SECONDS = 8.0
MAX_STOP_SECONDS = 600.0
ETA_SAMPLE_WINDOW = 40
ETA_MIN_SAMPLES = 3

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
    seconds_per_stop: float | None = None
    eta_samples: int = 0
    eta_learned: bool = False

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


@dataclass(slots=True)
class StopPace:
    """How long this bus takes between consecutive stops.

    The portal publishes no ETA — only which stop the bus is working on — so the
    only way to turn "8 stops away" into minutes is to time the bus ourselves.
    """

    samples: deque[float] = field(
        default_factory=lambda: deque(maxlen=ETA_SAMPLE_WINDOW)
    )
    last_stop: int | None = None
    last_seen: datetime | None = None

    @property
    def seconds_per_stop(self) -> float:
        """Average seconds between stops, falling back to a sane default."""
        if not self.samples:
            return DEFAULT_SECONDS_PER_STOP
        return sum(self.samples) / len(self.samples)

    @property
    def learned(self) -> bool:
        """Whether enough gaps have been timed to trust the average."""
        return len(self.samples) >= ETA_MIN_SAMPLES

    def observe(self, current_stop: int | None, now: datetime) -> None:
        """Record where the bus is, timing the gap since the previous stop."""
        if current_stop is None:
            return

        previous, seen = self.last_stop, self.last_seen
        self.last_stop, self.last_seen = current_stop, now

        if previous is None or seen is None or current_stop <= previous:
            # No baseline, or the numbering went backwards because a new run
            # started. Either way there is no gap worth timing.
            return

        advanced = current_stop - previous
        per_stop = (now - seen).total_seconds() / advanced
        if MIN_STOP_SECONDS <= per_stop <= MAX_STOP_SECONDS:
            self.samples.append(per_stop)

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
