"""Thin async client for the BusRight parent app's backend.

BusRight has no parent web portal; everything lives behind its mobile app. The
calls below are the ones that app makes, as captured from a parent's own
signed-in session:

* Sign-in is passwordless. ``POST /parent-app/auth/code`` emails a code, and
  ``POST /parent-app/auth/sign-in/code`` trades it for a Firebase *custom*
  token. The API itself wants a Firebase *ID* token, so the custom token is
  exchanged through Firebase exactly as the app does, which also yields a
  refresh token. That refresh token is all this integration stores.
* ``GET /user/districts`` lists every route in the parent's schools, and
  ``GET /parent-app/ridership/routes/{id}/parent/students`` answers only for
  routes one of their children rides, naming the stop(s) they use.
* ``GET /districts/{d}/schools/{s}/routes/{r}`` is the route: its stops in
  order, whether a run is in progress, and ``dbPath`` -- the Firestore document
  the driver's tablet writes the live position to.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.util import dt as dt_util

from .api import haversine_meters

_LOGGER = logging.getLogger(__name__)

API_HOST = "api.busright.com"
API_V2_HOST = "api-v2.busright.com"
REQUEST_CODE_PATH = "/parent-app/auth/code"
SIGN_IN_PATH = "/parent-app/auth/sign-in/code"
DISTRICTS_PATH = "/user/districts?schools=true&routesMeta=true"
STUDENTS_PATH = "/parent-app/ridership/routes/{route_id}/parent/students"
ROUTE_PATH = "/districts/{district_id}/schools/{school_id}/routes/{route_id}"

# BusRight is Firebase project busright-prod. This key ships inside every copy of
# the app; it is public client configuration, not a secret.
FIREBASE_API_KEY = "AIzaSyCw-DU-TeuATzgaCo2dKWgiJR_G_Vddz20"
FIREBASE_PROJECT = "busright-prod"
CUSTOM_TOKEN_URL = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken"
)
REFRESH_URL = "https://securetoken.googleapis.com/v1/token"
FIRESTORE_DOCS = (
    f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}"
    "/databases/(default)/documents"
)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
# ID tokens last an hour. Renew a little early so a poll never carries one that
# expires in flight.
TOKEN_MARGIN = timedelta(minutes=5)
# Discovery asks the students endpoint about every route in the parent's
# schools, one request each. Cap it so a large district cannot turn setup into
# hundreds of calls.
MAX_DISCOVERY_ROUTES = 60


class BusRightError(Exception):
    """Base error for the BusRight client."""


class BusRightConnectionError(BusRightError):
    """BusRight or Firebase could not be reached, or answered with an error."""

    def __init__(self, message: str, status: int | None = None) -> None:
        """Keep the HTTP status, when there was one."""
        super().__init__(message)
        self.status = status


class BusRightAuthError(BusRightError):
    """The stored sign-in is no longer accepted."""


class BusRightLoginError(BusRightError):
    """A step of the email-code sign-in failed.

    ``reason`` is a translation key for the config flow.
    """

    def __init__(self, reason: str, message: str = "") -> None:
        """Remember which error the flow should show."""
        super().__init__(message or reason)
        self.reason = reason


@dataclass(frozen=True)
class RouteRef:
    """Enough to find one route, plus whether a child of yours rides it."""

    district_id: str
    school_id: str
    route_id: str
    name: str
    stop_ids: tuple[str, ...] = ()

    @property
    def yours(self) -> bool:
        """Whether BusRight links one of your children to this route."""
        return bool(self.stop_ids)


@dataclass(frozen=True)
class RouteStop:
    """One stop on a route, in driving order."""

    index: int
    stop_id: str | None
    latitude: float
    longitude: float
    label: str
    # Seconds after the route's start, from BusRight's own routing. Districts
    # can hide these from parents in the app, but they still describe how long
    # the planner expects each leg to take.
    scheduled_offset: int | None


@dataclass
class Route:
    """A route as BusRight describes it, independent of any live position."""

    route_id: str
    name: str
    active: bool
    session_id: str | None
    db_path: str | None
    driver: str | None
    schedule_type: str | None
    stops: list[RouteStop] = field(default_factory=list)

    @classmethod
    def from_json(cls, route_id: str, payload: Any) -> Route:
        """Build from the route detail response."""
        detail = payload if isinstance(payload, dict) else {}
        data = detail.get("routeData")
        if not isinstance(data, dict):
            data = (detail.get("data") or {}).get("routeData") or {}
        status = data.get("status") if isinstance(data.get("status"), dict) else {}
        driver = data.get("assignedDriver") or {}

        stops: list[RouteStop] = []
        for raw in data.get("stops") or []:
            if not isinstance(raw, dict):
                continue
            coord = raw.get("coord") if isinstance(raw.get("coord"), dict) else {}
            lat = _as_float(raw.get("latitude", coord.get("latitude")))
            lng = _as_float(raw.get("longitude", coord.get("longitude")))
            if lat is None or lng is None:
                continue
            stops.append(
                RouteStop(
                    index=len(stops),
                    stop_id=raw.get("stopId"),
                    latitude=lat,
                    longitude=lng,
                    label=_address(raw.get("address")) or f"Stop {len(stops) + 1}",
                    scheduled_offset=_as_int(raw.get("eta")),
                )
            )

        return cls(
            route_id=route_id,
            name=data.get("name") or data.get("routeName") or route_id,
            active=bool(status.get("isActive")),
            session_id=status.get("sessionId"),
            db_path=data.get("dbPath"),
            driver=driver.get("preferredName") or driver.get("fullName") or None,
            schedule_type=data.get("scheduleType"),
            stops=stops,
        )


@dataclass(frozen=True)
class Position:
    """The bus's live position as last written by the driver's tablet."""

    latitude: float
    longitude: float
    updated: datetime | None
    raw: dict[str, Any]

    @property
    def age(self) -> timedelta | None:
        """How long ago this fix was written."""
        if self.updated is None:
            return None
        return dt_util.utcnow() - self.updated

    def distance_to(self, latitude: float, longitude: float) -> float | None:
        """Straight-line metres from the bus to a point."""
        return haversine_meters(self.latitude, self.longitude, latitude, longitude)


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


def _address(address: Any) -> str:
    """A short street-and-town label."""
    if not isinstance(address, dict):
        return ""
    parts = [address.get("street") or address.get("longStreet"), address.get("city")]
    return ", ".join(p for p in parts if p)


def decode_firestore(value: Any) -> Any:
    """Unwrap Firestore's REST encoding into plain Python values."""
    if not isinstance(value, dict):
        return value
    if "doubleValue" in value:
        return _as_float(value["doubleValue"])
    if "integerValue" in value:
        return _as_int(value["integerValue"])
    if "stringValue" in value:
        return value["stringValue"]
    if "booleanValue" in value:
        return value["booleanValue"]
    if "timestampValue" in value:
        return value["timestampValue"]
    if "nullValue" in value:
        return None
    if "geoPointValue" in value:
        point = value["geoPointValue"]
        return {"latitude": point.get("latitude"), "longitude": point.get("longitude")}
    if "mapValue" in value:
        return decode_fields(value["mapValue"].get("fields") or {})
    if "arrayValue" in value:
        return [decode_firestore(v) for v in value["arrayValue"].get("values") or []]
    return value


def decode_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Decode a Firestore ``fields`` map."""
    return {key: decode_firestore(val) for key, val in fields.items()}


def find_lat_lng(obj: Any, depth: int = 0) -> tuple[float, float] | None:
    """Find a latitude/longitude pair anywhere in a decoded document.

    Only the presence of the pair is known for certain -- the live position was
    first located by searching the document for it -- so search rather than
    assume a fixed nesting.
    """
    if not isinstance(obj, dict) or depth > 4:
        return None
    lat = _as_float(obj.get("latitude", obj.get("lat")))
    lng = _as_float(obj.get("longitude", obj.get("lng", obj.get("lon"))))
    if lat is not None and lng is not None and (lat, lng) != (0.0, 0.0):
        return lat, lng
    for value in obj.values():
        found = find_lat_lng(value, depth + 1)
        if found:
            return found
    return None


def parse_position(document: Any) -> Position | None:
    """Turn a Firestore document into a Position, if it holds one."""
    if not isinstance(document, dict):
        return None
    data = decode_fields(document.get("fields") or {})
    found = find_lat_lng(data)
    if found is None:
        return None
    # Firestore stamps every write with updateTime, which is the one timestamp
    # here that is certain to exist and to mean "when the tablet last wrote".
    updated = dt_util.parse_datetime(document.get("updateTime") or "")
    return Position(latitude=found[0], longitude=found[1], updated=updated, raw=data)


def parse_districts(payload: Any) -> list[RouteRef]:
    """Every route in every school this parent can see."""
    root = payload if isinstance(payload, dict) else {}
    if "districts" not in root and isinstance(root.get("data"), dict):
        root = root["data"]
    refs: list[RouteRef] = []
    seen: set[str] = set()
    for district in root.get("districts") or []:
        if not isinstance(district, dict):
            continue
        district_id = district.get("districtId") or (
            district.get("districtData") or {}
        ).get("id")
        for school in district.get("schools") or []:
            if not isinstance(school, dict):
                continue
            school_id = school.get("schoolId") or (school.get("schoolData") or {}).get("id")
            for route in school.get("routes") or []:
                if not isinstance(route, dict):
                    continue
                data = route.get("routeData") or {}
                route_id = route.get("routeId") or data.get("id") or route.get("id")
                if not (district_id and school_id and route_id) or route_id in seen:
                    continue
                seen.add(route_id)
                refs.append(
                    RouteRef(
                        district_id=district_id,
                        school_id=school_id,
                        route_id=route_id,
                        name=data.get("name") or route.get("name") or route_id,
                    )
                )
    return refs


def parse_student_stops(payload: Any) -> tuple[str, ...]:
    """The stop ids your children use on a route."""
    students = payload.get("data") if isinstance(payload, dict) else payload
    ids: list[str] = []
    for student in students if isinstance(students, list) else []:
        if not isinstance(student, dict):
            continue
        for stop_id in student.get("firebaseStopIds") or []:
            if stop_id not in ids:
                ids.append(stop_id)
    return tuple(ids)


class BusRightAuth:
    """Holds the Firebase tokens and renews the ID token as it expires."""

    def __init__(self, session: aiohttp.ClientSession, refresh_token: str) -> None:
        """Initialise from a stored refresh token."""
        self._session = session
        self.refresh_token = refresh_token
        self._id_token: str | None = None
        self._expires: datetime | None = None
        self._lock = asyncio.Lock()

    def seed(self, id_token: str, expires_in: Any) -> None:
        """Adopt an ID token just obtained at sign-in."""
        self._id_token = id_token
        self._expires = dt_util.utcnow() + timedelta(
            seconds=_as_int(expires_in) or 3600
        )

    def invalidate(self) -> None:
        """Forget the ID token so the next call renews it."""
        self._id_token = None

    async def async_id_token(self) -> str:
        """A current ID token, renewing it when it is close to expiry."""
        async with self._lock:
            if (
                self._id_token
                and self._expires
                and dt_util.utcnow() < self._expires - TOKEN_MARGIN
            ):
                return self._id_token
            payload = await _post_json(
                self._session,
                f"{REFRESH_URL}?key={FIREBASE_API_KEY}",
                {"grant_type": "refresh_token", "refresh_token": self.refresh_token},
                auth_statuses=(400, 401, 403),
            )
            id_token = payload.get("id_token")
            if not id_token:
                raise BusRightAuthError("Firebase did not return an ID token")
            # Firebase may rotate the refresh token; keep whichever is newest.
            self.refresh_token = payload.get("refresh_token") or self.refresh_token
            self.seed(id_token, payload.get("expires_in"))
            return id_token


async def _post_json(
    session: aiohttp.ClientSession,
    url: str,
    body: dict[str, Any],
    auth_statuses: tuple[int, ...] = (401, 403),
) -> dict[str, Any]:
    """POST JSON and return the decoded reply."""
    try:
        response = await session.post(url, json=body, timeout=REQUEST_TIMEOUT)
        if response.status in auth_statuses:
            raise BusRightAuthError(f"Rejected with HTTP {response.status}")
        if response.status >= 400:
            raise BusRightConnectionError(
                f"HTTP {response.status} from {url.split('?')[0]}", response.status
            )
        payload = await response.json(content_type=None)
    except aiohttp.ClientError as err:
        raise BusRightConnectionError(f"Cannot reach {url.split('?')[0]}: {err}") from err
    except asyncio.TimeoutError as err:
        raise BusRightConnectionError(f"Timed out talking to {url.split('?')[0]}") from err
    except ValueError as err:
        raise BusRightConnectionError("Reply was not JSON") from err
    return payload if isinstance(payload, dict) else {}


class BusRightLogin:
    """Walks BusRight's two-step email-code sign-in."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the login helper."""
        self._session = session

    async def async_request_code(self, email: str) -> None:
        """Ask BusRight to email a sign-in code."""
        try:
            await _post_json(
                self._session,
                f"https://{API_V2_HOST}{REQUEST_CODE_PATH}",
                {"email": email},
                auth_statuses=(),
            )
        except BusRightConnectionError as err:
            if err.status is not None and err.status < 500:
                raise BusRightLoginError("cannot_request_code", str(err)) from err
            raise

    async def async_sign_in(self, email: str, code: str) -> BusRightAuth:
        """Trade the emailed code for a signed-in token holder."""
        try:
            payload = await _post_json(
                self._session,
                f"https://{API_V2_HOST}{SIGN_IN_PATH}",
                {"email": email, "code": code.strip()},
                auth_statuses=(),
            )
        except BusRightConnectionError as err:
            if err.status is not None and err.status < 500:
                raise BusRightLoginError("invalid_code", str(err)) from err
            raise
        custom = ((payload.get("data") or {}).get("auth") or {}).get("token")
        if not custom:
            raise BusRightLoginError("invalid_code", "No token in sign-in reply")

        try:
            exchanged = await _post_json(
                self._session,
                f"{CUSTOM_TOKEN_URL}?key={FIREBASE_API_KEY}",
                {"token": custom, "returnSecureToken": True},
                auth_statuses=(400, 401, 403),
            )
        except BusRightAuthError as err:
            raise BusRightLoginError("unexpected_response", str(err)) from err
        if not exchanged.get("idToken") or not exchanged.get("refreshToken"):
            raise BusRightLoginError("unexpected_response", "Token exchange failed")

        auth = BusRightAuth(self._session, exchanged["refreshToken"])
        auth.seed(exchanged["idToken"], exchanged.get("expiresIn"))
        return auth


class BusRightApi:
    """Signed-in calls to BusRight and its Firestore."""

    def __init__(self, session: aiohttp.ClientSession, auth: BusRightAuth) -> None:
        """Initialise the client."""
        self._session = session
        self.auth = auth

    async def _get(
        self, url: str, *, missing_ok: bool = False, refused_ok: bool = False
    ) -> Any:
        """GET with a bearer token, renewing it once if it is refused.

        ``refused_ok`` treats a 401 as "not yours" (returns None) instead of a
        dead sign-in, for endpoints that answer 401 to a valid token that lacks
        access to that one resource.
        """
        for attempt in (1, 2):
            token = await self.auth.async_id_token()
            try:
                response = await self._session.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                    timeout=REQUEST_TIMEOUT,
                )
                if response.status == 401 and refused_ok:
                    _LOGGER.debug(
                        "BusRight refused %s with HTTP 401; treating it as not yours",
                        url.split("?")[0],
                    )
                    return None
                if response.status == 401 and attempt == 1:
                    self.auth.invalidate()
                    continue
                if response.status == 401:
                    _LOGGER.warning(
                        "BusRight refused %s with HTTP 401 after renewing the "
                        "sign-in: %s",
                        url.split("?")[0],
                        (await response.text())[:200],
                    )
                    raise BusRightAuthError("Signed-in token rejected")
                if response.status in (403, 404) and missing_ok:
                    return None
                if response.status >= 400:
                    raise BusRightConnectionError(
                        f"HTTP {response.status} from {url.split('?')[0]}",
                        response.status,
                    )
                return await response.json(content_type=None)
            except aiohttp.ClientError as err:
                raise BusRightConnectionError(f"Cannot reach BusRight: {err}") from err
            except asyncio.TimeoutError as err:
                raise BusRightConnectionError("Timed out talking to BusRight") from err
            except ValueError as err:
                raise BusRightConnectionError("BusRight reply was not JSON") from err
        raise BusRightAuthError("Signed-in token rejected")

    async def async_discover_routes(self) -> list[RouteRef]:
        """Every route you can see, marking the ones your children ride.

        The district listing names every route in your children's schools, not
        just theirs. The students call answers only for routes they ride, which
        is what separates the two. A route can still be yours with no student
        linked -- the school simply has not filled that in -- so nothing is
        dropped here; the caller decides.
        """
        refs = parse_districts(await self._get(f"https://{API_HOST}{DISTRICTS_PATH}"))
        if len(refs) > MAX_DISCOVERY_ROUTES:
            _LOGGER.warning(
                "BusRight lists %d routes; only checking the first %d for your "
                "children", len(refs), MAX_DISCOVERY_ROUTES,
            )
        out: list[RouteRef] = []
        for ref in refs[:MAX_DISCOVERY_ROUTES]:
            payload = await self._get(
                f"https://{API_V2_HOST}"
                + STUDENTS_PATH.format(route_id=ref.route_id),
                missing_ok=True,
                # The students endpoint answers 401 "unauthorized" for routes
                # your children don't ride, even with a good sign-in.
                refused_ok=True,
            )
            stop_ids = parse_student_stops(payload) if payload else ()
            out.append(
                RouteRef(ref.district_id, ref.school_id, ref.route_id, ref.name, stop_ids)
            )
        return out

    async def async_get_route(
        self, district_id: str, school_id: str, route_id: str
    ) -> Route:
        """The route's stops and whether a run is under way."""
        payload = await self._get(
            f"https://{API_HOST}"
            + ROUTE_PATH.format(
                district_id=district_id, school_id=school_id, route_id=route_id
            )
        )
        return Route.from_json(route_id, payload)

    async def async_get_position(self, db_path: str) -> Position | None:
        """The live position document a route's ``dbPath`` points at."""
        document = await self._get(f"{FIRESTORE_DOCS}/{db_path}", missing_ok=True)
        return parse_position(document)
