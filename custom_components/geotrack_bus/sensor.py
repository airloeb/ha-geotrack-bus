"""Sensors for buses and for your own stops."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from math import asin, cos, radians, sin, sqrt
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    DEGREE,
    UnitOfLength,
    UnitOfSpeed,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import (
    Bus,
    STATUS_APPROACHING,
    STATUS_AT_STOP,
    STATUS_PASSED,
    STATUS_UNKNOWN,
    Stop,
)
from .coordinator import GeoTrackConfigEntry, GeoTrackCoordinator
from .entity import GeoTrackBusEntity, GeoTrackStopEntity

COMPASS = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)

EARTH_RADIUS_M = 6371000.0


def _compass(bearing: int | None) -> str | None:
    """Turn a bearing in degrees into a 16-point compass label."""
    if bearing is None:
        return None
    return COMPASS[round(bearing % 360 / 22.5) % 16]


def _distance_m(
    lat1: float | None, lon1: float | None, lat2: float | None, lon2: float | None
) -> float | None:
    """Great-circle distance in metres between two positions."""
    if None in (lat1, lon1, lat2, lon2):
        return None
    p1, p2 = radians(lat1), radians(lat2)
    dp, dl = p2 - p1, radians(lon2 - lon1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return round(2 * EARTH_RADIUS_M * asin(sqrt(a)), 1)


@dataclass(frozen=True, kw_only=True)
class GeoTrackBusSensorDescription(SensorEntityDescription):
    """Describes a sensor derived from the bus itself."""

    value_fn: Callable[[Bus], Any]
    attributes_fn: Callable[[Bus], dict[str, Any]] | None = None


@dataclass(frozen=True, kw_only=True)
class GeoTrackStopSensorDescription(SensorEntityDescription):
    """Describes a sensor derived from one of your stops."""

    value_fn: Callable[[Bus, Stop], Any]
    attributes_fn: Callable[[Bus, Stop], dict[str, Any]] | None = None


BUS_SENSORS: tuple[GeoTrackBusSensorDescription, ...] = (
    GeoTrackBusSensorDescription(
        key="speed",
        translation_key="speed",
        device_class=SensorDeviceClass.SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfSpeed.MILES_PER_HOUR,
        value_fn=lambda bus: bus.speed,
    ),
    GeoTrackBusSensorDescription(
        key="bearing",
        translation_key="bearing",
        icon="mdi:compass-outline",
        native_unit_of_measurement=DEGREE,
        value_fn=lambda bus: bus.bearing,
        attributes_fn=lambda bus: {"direction": _compass(bus.bearing)},
    ),
    GeoTrackBusSensorDescription(
        key="address",
        translation_key="address",
        icon="mdi:map-marker",
        value_fn=lambda bus: bus.address,
    ),
    GeoTrackBusSensorDescription(
        key="route",
        translation_key="route",
        icon="mdi:sign-direction",
        value_fn=lambda bus: bus.route,
    ),
    GeoTrackBusSensorDescription(
        key="last_report",
        translation_key="last_report",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda bus: bus.last_update,
    ),
)

STOP_SENSORS: tuple[GeoTrackStopSensorDescription, ...] = (
    GeoTrackStopSensorDescription(
        key="stops_away",
        translation_key="stops_away",
        icon="mdi:bus-stop",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="stops",
        value_fn=lambda bus, stop: stop.stops_away,
        attributes_fn=lambda bus, stop: {
            "your_stop_number": stop.stop_number,
            "bus_at_stop_number": stop.current_stop_number,
        },
    ),
    GeoTrackStopSensorDescription(
        key="status",
        translation_key="status",
        icon="mdi:bus-clock",
        device_class=SensorDeviceClass.ENUM,
        options=[STATUS_APPROACHING, STATUS_AT_STOP, STATUS_PASSED, STATUS_UNKNOWN],
        value_fn=lambda bus, stop: stop.status,
        attributes_fn=lambda bus, stop: {
            "passed_at": stop.passed_at,
            "stop_address": stop.stop_address,
        },
    ),
    GeoTrackStopSensorDescription(
        key="distance",
        translation_key="distance",
        icon="mdi:map-marker-distance",
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfLength.METERS,
        suggested_unit_of_measurement=UnitOfLength.MILES,
        suggested_display_precision=2,
        value_fn=lambda bus, stop: _distance_m(
            bus.latitude, bus.longitude, stop.latitude, stop.longitude
        ),
    ),
    GeoTrackStopSensorDescription(
        key="message",
        translation_key="message",
        icon="mdi:message-text",
        value_fn=lambda bus, stop: stop.message[:255] or None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GeoTrackConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up bus and stop sensors, adding them as new ones appear in the feed."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _async_add_new() -> None:
        new: list[SensorEntity] = []
        for bus in (coordinator.data or {}).values():
            for bus_desc in BUS_SENSORS:
                uid = f"{bus.bus_id}_{bus_desc.key}"
                if uid in known:
                    continue
                known.add(uid)
                new.append(GeoTrackBusSensor(coordinator, bus.bus_id, bus_desc))
            for stop in bus.stops:
                for stop_desc in STOP_SENSORS:
                    uid = f"{bus.bus_id}_{stop.key}_{stop_desc.key}"
                    if uid in known:
                        continue
                    known.add(uid)
                    new.append(
                        GeoTrackStopSensor(coordinator, bus.bus_id, stop, stop_desc)
                    )
        if new:
            async_add_entities(new)

    _async_add_new()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new))


class GeoTrackBusSensor(GeoTrackBusEntity, SensorEntity):
    """A sensor reading straight off the vehicle record."""

    entity_description: GeoTrackBusSensorDescription

    def __init__(
        self,
        coordinator: GeoTrackCoordinator,
        bus_id: int,
        description: GeoTrackBusSensorDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, bus_id)
        self.entity_description = description
        self._attr_unique_id = f"{bus_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        bus = self.bus
        return self.entity_description.value_fn(bus) if bus else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes, if this sensor defines any."""
        bus = self.bus
        if bus is None or self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(bus)


class GeoTrackStopSensor(GeoTrackStopEntity, SensorEntity):
    """A sensor describing the bus's progress towards one of your stops."""

    entity_description: GeoTrackStopSensorDescription

    def __init__(
        self,
        coordinator: GeoTrackCoordinator,
        bus_id: int,
        stop: Stop,
        description: GeoTrackStopSensorDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, bus_id, stop.key)
        self.entity_description = description
        self._attr_unique_id = f"{bus_id}_{stop.key}_{description.key}"
        self._attr_translation_placeholders = {"stop": stop.label}

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        bus, stop = self.bus, self.stop
        if bus is None or stop is None:
            return None
        return self.entity_description.value_fn(bus, stop)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes, if this sensor defines any."""
        bus, stop = self.bus, self.stop
        if bus is None or stop is None or self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(bus, stop)
