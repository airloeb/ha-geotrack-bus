"""Live bus positions on the Home Assistant map."""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import GeoTrackConfigEntry, GeoTrackCoordinator
from .entity import GeoTrackBusEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GeoTrackConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one tracker per bus, adding buses as they appear."""
    coordinator = entry.runtime_data
    known: set[int] = set()

    @callback
    def _async_add_new() -> None:
        new: list[GeoTrackBusTracker] = []
        for bus_id in coordinator.data or {}:
            if bus_id in known:
                continue
            known.add(bus_id)
            new.append(GeoTrackBusTracker(coordinator, bus_id))
        if new:
            async_add_entities(new)

    _async_add_new()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new))


class GeoTrackBusTracker(GeoTrackBusEntity, TrackerEntity):
    """Reports the bus's GPS position."""

    _attr_name = None
    _attr_icon = "mdi:bus"

    def __init__(self, coordinator: GeoTrackCoordinator, bus_id: int) -> None:
        """Initialise the tracker."""
        super().__init__(coordinator, bus_id)
        self._attr_unique_id = f"{bus_id}_tracker"

    @property
    def source_type(self) -> SourceType:
        """Positions come from the vehicle's GPS unit."""
        return SourceType.GPS

    @property
    def latitude(self) -> float | None:
        """Latitude of the bus."""
        return self.bus.latitude if self.bus else None

    @property
    def longitude(self) -> float | None:
        """Longitude of the bus."""
        return self.bus.longitude if self.bus else None

    @property
    def location_accuracy(self) -> int:
        """Accuracy in metres; the portal does not report one."""
        return 25

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the rest of the feed for templates and dashboards."""
        bus = self.bus
        if bus is None:
            return {}
        return {
            "bus_id": bus.bus_id,
            "bus_number": bus.bus_number,
            "route": bus.route,
            "speed": bus.speed,
            "bearing": bus.bearing,
            "address": bus.address,
            "last_update": bus.last_update.isoformat() if bus.last_update else None,
            "point_type": bus.point_type,
            "stops": [
                {
                    "stop_number": stop.stop_number,
                    "status": stop.status,
                    "stops_away": stop.stops_away,
                    "message": stop.message,
                    "latitude": stop.latitude,
                    "longitude": stop.longitude,
                }
                for stop in bus.stops
            ],
        }
