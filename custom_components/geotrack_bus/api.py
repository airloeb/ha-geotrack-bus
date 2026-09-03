"""Thin async client for the GeoTrack parent portal."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

VEHICLES_PATH = "/Map/GetVehicles"
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
