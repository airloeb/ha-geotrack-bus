"""Entities for BusRight routes.

Kept apart from the GeoTrack entities because the two services describe a bus
differently: GeoTrack hands over a vehicle with messages attached, BusRight a
route with a position. The stop entities deliberately mirror GeoTrack's --
same keys, same status values -- so automations read the same either way.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfLength, UnitOfSpeed, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import STATUS_APPROACHING, STATUS_AT_STOP, STATUS_PASSED, STATUS_UNKNOWN
from .busright_coordinator import (
    STATUS_NOT_RUNNING,
    BusRightConfigEntry,
    BusRightCoordinator,
    RouteStatus,
)
from .const import (
    BUSRIGHT_MANUFACTURER,
    CONF_WARNING_MILES,
    CONF_WARNING_STOPS,
    DEFAULT_WARNING_MILES,
    DEFAULT_WARNING_STOPS,
    DOMAIN,
)

MILES = 1609.344


def _miles(metres: float | None) -> float | None:
    """Metres as miles, for attributes a human will read."""
    return None if metres is None else round(metres / MILES, 2)


def _minutes(status: RouteStatus) -> int | None:
    """Age of the last position, in whole minutes."""
    age = status.position.age if status.position else None
    return None if age is None else round(age.total_seconds() / 60)


class BusRightRouteEntity(CoordinatorEntity[BusRightCoordinator]):
    """Base entity attached to a route's device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: BusRightCoordinator, route_id: str) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        self._route_id = route_id
        name = coordinator.statuses[route_id].watched.name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"busright:{route_id}")},
            manufacturer=BUSRIGHT_MANUFACTURER,
            name=f"BusRight {name}",
            model="School bus route",
        )

    @property
    def status(self) -> RouteStatus:
        """The route this entity belongs to."""
        return self.coordinator.statuses[self._route_id]

    @property
    def available(self) -> bool:
        """Available once the route has been read at least once."""
        return super().available and self.status.route is not None


class BusRightStopEntity(CoordinatorEntity[BusRightCoordinator]):
    """Base entity for one of your stops.

    One stop can be on several routes -- the morning pick-up and the afternoon
    drop-off -- so it resolves whichever is most relevant right now: a run that
    is still heading for the stop, then any run in progress, then the most
    recently heard from.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: BusRightCoordinator, stop_key: str) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        self._stop_key = stop_key
        label = next(
            s.watched.stop_label
            for s in coordinator.statuses.values()
            if s.watched.stop_key == stop_key
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"busright_stop:{stop_key}")},
            manufacturer=BUSRIGHT_MANUFACTURER,
            name=f"BusRight stop {label.split(',')[0]}",
            model="Bus stop",
        )

    @property
    def status(self) -> RouteStatus | None:
        """The route serving this stop that matters most at the moment."""
        candidates = [
            s for s in self.coordinator.statuses.values()
            if s.watched.stop_key == self._stop_key and s.route is not None
        ]
        if not candidates:
            return None

        def rank(s: RouteStatus) -> tuple[int, int, float]:
            heading = s.status in (STATUS_APPROACHING, STATUS_AT_STOP)
            updated = s.position.updated if s.position else None
            seen = updated.timestamp() if updated else 0.0
            return (0 if heading else 1, 0 if s.running else 1, -seen)

        return min(candidates, key=rank)

    @property
    def available(self) -> bool:
        """Available once any of its routes has been read."""
        return super().available and self.status is not None


# --- sensors ---------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class BusRightSensorDescription(SensorEntityDescription):
    """A sensor computed from a route's status."""

    value_fn: Callable[[RouteStatus], Any]
    attributes_fn: Callable[[RouteStatus], dict[str, Any]] | None = None


ROUTE_SENSORS: tuple[BusRightSensorDescription, ...] = (
    BusRightSensorDescription(
        key="speed",
        translation_key="speed",
        device_class=SensorDeviceClass.SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        suggested_unit_of_measurement=UnitOfSpeed.MILES_PER_HOUR,
        suggested_display_precision=0,
        value_fn=lambda s: (
            s.position.speed if s.position and not s.position_stale else None
        ),
    ),
    BusRightSensorDescription(
        key="driver",
        translation_key="driver",
        icon="mdi:card-account-details-outline",
        value_fn=lambda s: s.route.driver if s.route else None,
    ),
    BusRightSensorDescription(
        key="last_report",
        translation_key="last_report",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: s.position.updated if s.position else None,
        attributes_fn=lambda s: {"position_stale": s.position_stale},
    ),
)

STOP_SENSORS: tuple[BusRightSensorDescription, ...] = (
    BusRightSensorDescription(
        key="status",
        translation_key="status",
        icon="mdi:bus-clock",
        device_class=SensorDeviceClass.ENUM,
        options=[
            STATUS_APPROACHING,
            STATUS_AT_STOP,
            STATUS_PASSED,
            STATUS_NOT_RUNNING,
            STATUS_UNKNOWN,
        ],
        value_fn=lambda s: s.status,
        attributes_fn=lambda s: {
            "route": s.watched.name,
            "stop_address": s.watched.stop_label,
            "passed_at": s.passed_at.isoformat() if s.passed_at else None,
            "skipped": s.skipped,
            "from_busright": s.from_busright,
            "last_stop_reached": s.last_reached,
            "running": s.running,
            "position_stale": s.position_stale,
            "stop_on_route": s.my_index is not None,
        },
    ),
    BusRightSensorDescription(
        key="distance",
        translation_key="distance",
        icon="mdi:map-marker-distance",
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfLength.METERS,
        suggested_unit_of_measurement=UnitOfLength.MILES,
        suggested_display_precision=2,
        value_fn=lambda s: s.distance_m,
    ),
    BusRightSensorDescription(
        key="stops_away",
        translation_key="stops_away",
        icon="mdi:bus-stop",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="stops",
        value_fn=lambda s: s.stops_away,
        attributes_fn=lambda s: {
            "your_stop_number": None if s.my_index is None else s.my_index + 1,
            "stops_on_route": s.stop_count,
            "last_stop_reached": s.last_reached,
        },
    ),
    BusRightSensorDescription(
        key="eta",
        translation_key="eta",
        icon="mdi:timer-sand",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=0,
        value_fn=lambda s: s.eta_minutes,
        attributes_fn=lambda s: {
            # "busright" is BusRight's own live ETA, as its app shows; "planned"
            # is its route plan, counted from the last stop reached.
            "source": s.eta_source,
            "last_stop_reached": s.last_reached,
        },
    ),
    BusRightSensorDescription(
        key="serving_route",
        translation_key="serving_route",
        icon="mdi:sign-direction",
        value_fn=lambda s: s.route.name if s.route else s.watched.name,
        attributes_fn=lambda s: {
            "driver": s.route.driver if s.route else None,
            "schedule": s.route.schedule_type if s.route else None,
        },
    ),
)


class BusRightRouteSensor(BusRightRouteEntity, SensorEntity):
    """A sensor describing the route or its bus."""

    entity_description: BusRightSensorDescription

    def __init__(
        self,
        coordinator: BusRightCoordinator,
        route_id: str,
        description: BusRightSensorDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, route_id)
        self.entity_description = description
        self._attr_unique_id = f"busright_{route_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.status)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes, if this sensor defines any."""
        if self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(self.status)


class BusRightStopSensor(BusRightStopEntity, SensorEntity):
    """A sensor describing the bus's progress towards one of your stops."""

    entity_description: BusRightSensorDescription

    def __init__(
        self,
        coordinator: BusRightCoordinator,
        stop_key: str,
        description: BusRightSensorDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, stop_key)
        self.entity_description = description
        self._attr_unique_id = f"busright_stop{stop_key}_{description.key}"

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        status = self.status
        return self.entity_description.value_fn(status) if status else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes, if this sensor defines any."""
        status = self.status
        if status is None or self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(status)


# --- binary sensors --------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class BusRightBinaryDescription(BinarySensorEntityDescription):
    """A binary sensor computed from a route's status."""

    value_fn: Callable[[RouteStatus], bool | None]


ROUTE_BINARY_SENSORS: tuple[BusRightBinaryDescription, ...] = (
    BusRightBinaryDescription(
        key="running",
        translation_key="running",
        icon="mdi:bus-school",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda s: s.running,
    ),
    BusRightBinaryDescription(
        key="moving",
        translation_key="moving",
        device_class=BinarySensorDeviceClass.MOVING,
        value_fn=lambda s: s.moving,
    ),
)

STOP_BINARY_SENSORS: tuple[BusRightBinaryDescription, ...] = (
    BusRightBinaryDescription(
        key="arriving_soon",
        translation_key="arriving_soon",
        icon="mdi:bus-clock",
        value_fn=lambda s: s.warning is not None,
    ),
    BusRightBinaryDescription(
        key="at_stop",
        translation_key="at_stop",
        icon="mdi:bus-marker",
        value_fn=lambda s: s.status == STATUS_AT_STOP,
    ),
    BusRightBinaryDescription(
        key="already_passed",
        translation_key="already_passed",
        icon="mdi:bus-side",
        value_fn=lambda s: s.status == STATUS_PASSED,
    ),
)


class BusRightRouteBinarySensor(BusRightRouteEntity, BinarySensorEntity):
    """A binary sensor describing the route or its bus."""

    entity_description: BusRightBinaryDescription

    def __init__(
        self,
        coordinator: BusRightCoordinator,
        route_id: str,
        description: BusRightBinaryDescription,
    ) -> None:
        """Initialise the binary sensor."""
        super().__init__(coordinator, route_id)
        self.entity_description = description
        self._attr_unique_id = f"busright_{route_id}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        """Return the binary sensor state."""
        return self.entity_description.value_fn(self.status)


class BusRightStopBinarySensor(BusRightStopEntity, BinarySensorEntity):
    """A binary sensor describing the bus's progress towards one of your stops."""

    entity_description: BusRightBinaryDescription

    def __init__(
        self,
        coordinator: BusRightCoordinator,
        stop_key: str,
        description: BusRightBinaryDescription,
    ) -> None:
        """Initialise the binary sensor."""
        super().__init__(coordinator, stop_key)
        self.entity_description = description
        self._attr_unique_id = f"busright_stop{stop_key}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        """Return the binary sensor state."""
        status = self.status
        if status is None:
            return None
        return self.entity_description.value_fn(status)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Surface what is behind an arriving-soon alert."""
        status = self.status
        if status is None or self.entity_description.key != "arriving_soon":
            return None
        options = self.coordinator.config_entry.options or {}
        return {
            "triggered_by": status.warning,
            "distance_miles": _miles(status.distance_m),
            "stops_away": status.stops_away,
            "eta_minutes": status.eta_minutes,
            "eta_source": status.eta_source,
            "warning_miles": options.get(CONF_WARNING_MILES, DEFAULT_WARNING_MILES),
            "warning_stops": options.get(CONF_WARNING_STOPS, DEFAULT_WARNING_STOPS),
            "route": status.watched.name,
        }


# --- map -------------------------------------------------------------------


class BusRightTracker(BusRightRouteEntity, TrackerEntity):
    """The bus's live position on the map."""

    _attr_name = None
    _attr_icon = "mdi:bus"

    def __init__(self, coordinator: BusRightCoordinator, route_id: str) -> None:
        """Initialise the tracker."""
        super().__init__(coordinator, route_id)
        self._attr_unique_id = f"busright_{route_id}_tracker"

    @property
    def force_update(self) -> bool:
        """Write the state only when it changes, like any other entity.

        Home Assistant's trackers force every write by default, which resets
        last_changed on each poll even while the bus sits in one zone, and
        breaks "has been at school for N minutes" conditions.
        """
        return False

    @property
    def source_type(self) -> SourceType:
        """Positions come from the driver's tablet GPS."""
        return SourceType.GPS

    @property
    def latitude(self) -> float | None:
        """Latitude of the bus."""
        position = self.status.position
        return position.latitude if position else None

    @property
    def longitude(self) -> float | None:
        """Longitude of the bus."""
        position = self.status.position
        return position.longitude if position else None

    @property
    def location_accuracy(self) -> int:
        """Accuracy in metres; BusRight does not report one."""
        return 25

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the rest of the feed for templates and dashboards."""
        status = self.status
        position = status.position
        return {
            "route": status.watched.name,
            "driver": status.route.driver if status.route else None,
            "running": status.running,
            "position_stale": status.position_stale,
            "position_age_minutes": _minutes(status),
            "speed_mph": (
                None if position is None or position.speed is None
                else round(position.speed * 2.23694)
            ),
            "bearing": position.bearing if position else None,
        }


# --- platform setup --------------------------------------------------------


def _stop_keys(coordinator: BusRightCoordinator) -> list[str]:
    """Each of your stops once, however many routes serve it."""
    keys: list[str] = []
    for status in coordinator.statuses.values():
        if status.watched.stop_key not in keys:
            keys.append(status.watched.stop_key)
    return keys


async def async_setup_sensors(
    hass: HomeAssistant,
    entry: BusRightConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add route and stop sensors."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        BusRightRouteSensor(coordinator, route_id, desc)
        for route_id in coordinator.statuses
        for desc in ROUTE_SENSORS
    ]
    entities += [
        BusRightStopSensor(coordinator, key, desc)
        for key in _stop_keys(coordinator)
        for desc in STOP_SENSORS
    ]
    async_add_entities(entities)


async def async_setup_binary_sensors(
    hass: HomeAssistant,
    entry: BusRightConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add route and stop binary sensors."""
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = [
        BusRightRouteBinarySensor(coordinator, route_id, desc)
        for route_id in coordinator.statuses
        for desc in ROUTE_BINARY_SENSORS
    ]
    entities += [
        BusRightStopBinarySensor(coordinator, key, desc)
        for key in _stop_keys(coordinator)
        for desc in STOP_BINARY_SENSORS
    ]
    async_add_entities(entities)


async def async_setup_trackers(
    hass: HomeAssistant,
    entry: BusRightConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one map marker per route."""
    coordinator = entry.runtime_data
    async_add_entities(
        BusRightTracker(coordinator, route_id) for route_id in coordinator.statuses
    )
