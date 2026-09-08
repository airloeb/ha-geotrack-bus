"""Binary sensors for bus movement and stop progress."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import STATUS_APPROACHING, STATUS_AT_STOP, STATUS_PASSED, Bus, Stop
from .const import (
    CONF_WARNING_MILES,
    CONF_WARNING_MINUTES,
    CONF_WARNING_STOPS,
    DEFAULT_WARNING_MILES,
    DEFAULT_WARNING_MINUTES,
    DEFAULT_WARNING_STOPS,
)
from .coordinator import GeoTrackConfigEntry, GeoTrackCoordinator
from .entity import GeoTrackBusEntity, GeoTrackStopEntity


@dataclass(frozen=True, kw_only=True)
class GeoTrackBusBinaryDescription(BinarySensorEntityDescription):
    """Describes a binary sensor derived from the bus itself."""

    value_fn: Callable[[Bus], bool | None]


@dataclass(frozen=True, kw_only=True)
class GeoTrackStopBinaryDescription(BinarySensorEntityDescription):
    """Describes a binary sensor derived from one of your stops."""

    value_fn: Callable[[Bus, Stop], bool | None]


BUS_BINARY_SENSORS: tuple[GeoTrackBusBinaryDescription, ...] = (
    GeoTrackBusBinaryDescription(
        key="moving",
        translation_key="moving",
        device_class=BinarySensorDeviceClass.MOVING,
        value_fn=lambda bus: None if bus.speed is None else bus.speed > 0,
    ),
)

MILES = 1609.344


def _arriving_soon(coordinator: GeoTrackCoordinator, stop: Stop) -> bool:
    """Whether the bus is close enough to be worth telling someone about.

    Driven by fixed thresholds, not by the learned estimate, so it works on the
    first run of a route rather than after a week of calibration. Two signals,
    whichever trips first:

    * **Distance.** Works everywhere. Measured at 0.49 mi four minutes before
      arrival, so the 0.75 mi default lands around five minutes out.
    * **Stops away.** Exact, portal-supplied, and immune to a frozen feed --
      but only useful where the rider is far enough down the route to have
      earlier stops to count. A rider at stop 1 reads 0 for the whole approach,
      and at stop 3 the portal never advanced past "before stop 1" at all, so
      the rule is skipped unless the runway genuinely exists.
    """
    if stop.status != STATUS_APPROACHING:
        return False

    options = coordinator.config_entry.options or {}
    miles = float(options.get(CONF_WARNING_MILES, DEFAULT_WARNING_MILES))
    stops = int(options.get(CONF_WARNING_STOPS, DEFAULT_WARNING_STOPS))

    if stop.distance_m is not None and stop.distance_m <= miles * MILES:
        return True

    # Needs at least one stop before the threshold for the count to mean
    # anything; otherwise it would be true from the moment the run began.
    has_runway = stop.stop_number is not None and stop.stop_number > stops + 1
    if has_runway and stop.stops_away is not None and stop.stops_away <= stops:
        return True

    return False


STOP_BINARY_SENSORS: tuple[GeoTrackStopBinaryDescription, ...] = (
    GeoTrackStopBinaryDescription(
        key="arriving_soon",
        translation_key="arriving_soon",
        icon="mdi:bus-clock",
        value_fn=lambda bus, stop: None,  # replaced per-entity; see is_on below
    ),
    GeoTrackStopBinaryDescription(
        key="at_stop",
        translation_key="at_stop",
        icon="mdi:bus-marker",
        value_fn=lambda bus, stop: stop.status == STATUS_AT_STOP,
    ),
    GeoTrackStopBinaryDescription(
        key="already_passed",
        translation_key="already_passed",
        icon="mdi:bus-side",
        value_fn=lambda bus, stop: stop.status == STATUS_PASSED,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GeoTrackConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors, adding them as new buses and stops appear."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _async_add_new() -> None:
        new: list[BinarySensorEntity] = []
        for bus in (coordinator.data or {}).values():
            for bus_desc in BUS_BINARY_SENSORS:
                uid = f"{bus.bus_id}_{bus_desc.key}"
                if uid in known:
                    continue
                known.add(uid)
                new.append(GeoTrackBusBinarySensor(coordinator, bus.bus_id, bus_desc))
            for stop in bus.stops:
                for stop_desc in STOP_BINARY_SENSORS:
                    uid = f"stop{stop.slug}_{stop_desc.key}"
                    if uid in known:
                        continue
                    known.add(uid)
                    new.append(
                        GeoTrackStopBinarySensor(coordinator, stop, stop_desc)
                    )
        if new:
            async_add_entities(new)

    _async_add_new()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new))


class GeoTrackBusBinarySensor(GeoTrackBusEntity, BinarySensorEntity):
    """A binary sensor reading straight off the vehicle record."""

    entity_description: GeoTrackBusBinaryDescription

    def __init__(
        self,
        coordinator: GeoTrackCoordinator,
        bus_id: int,
        description: GeoTrackBusBinaryDescription,
    ) -> None:
        """Initialise the binary sensor."""
        super().__init__(coordinator, bus_id)
        self.entity_description = description
        self._attr_unique_id = f"{bus_id}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        """Return the binary sensor state."""
        bus = self.bus
        return self.entity_description.value_fn(bus) if bus else None


class GeoTrackStopBinarySensor(GeoTrackStopEntity, BinarySensorEntity):
    """A binary sensor describing the bus's progress towards one of your stops."""

    entity_description: GeoTrackStopBinaryDescription

    def __init__(
        self,
        coordinator: GeoTrackCoordinator,
        stop: Stop,
        description: GeoTrackStopBinaryDescription,
    ) -> None:
        """Initialise the binary sensor."""
        super().__init__(coordinator, stop)
        self.entity_description = description
        self._attr_unique_id = f"stop{stop.slug}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        """Return the binary sensor state."""
        bus, stop = self.bus, self.stop
        if bus is None or stop is None:
            return None
        if self.entity_description.key == "arriving_soon":
            return _arriving_soon(self.coordinator, stop)
        return self.entity_description.value_fn(bus, stop)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Surface the estimate behind an arriving-soon alert."""
        stop = self.stop
        if stop is None or self.entity_description.key != "arriving_soon":
            return None
        return {
            "eta_minutes": stop.eta_minutes,
            "stops_away": stop.stops_away,
            "warning_miles": (self.coordinator.config_entry.options or {}).get(
                CONF_WARNING_MILES, DEFAULT_WARNING_MILES
            ),
            "warning_stops": (self.coordinator.config_entry.options or {}).get(
                CONF_WARNING_STOPS, DEFAULT_WARNING_STOPS
            ),
            "triggered_by": (
                "distance"
                if stop.distance_m is not None
                and stop.distance_m
                <= float((self.coordinator.config_entry.options or {}).get(
                    CONF_WARNING_MILES, DEFAULT_WARNING_MILES)) * 1609.344
                else "stops_away"
            ),
            "warning_at_miles": (
                None if stop.warning_distance_m is None
                else round(stop.warning_distance_m / 1609.344, 2)
            ),
            "distance_miles": (
                None if stop.distance_m is None
                else round(stop.distance_m / 1609.344, 2)
            ),
            "runs_measured": stop.eta_runs,
            "route": stop.route,
        }
