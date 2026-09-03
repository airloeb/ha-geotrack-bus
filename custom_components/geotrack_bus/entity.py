"""Shared entity plumbing for GeoTrack Bus Tracking."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import Bus, Stop
from .const import DOMAIN, MANUFACTURER
from .coordinator import GeoTrackCoordinator


class GeoTrackBusEntity(CoordinatorEntity[GeoTrackCoordinator]):
    """Base entity attached to a bus device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: GeoTrackCoordinator, bus_id: int) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        self._bus_id = bus_id
        bus = self.bus
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(bus_id))},
            manufacturer=MANUFACTURER,
            name=bus.name if bus else f"Bus {bus_id}",
            model="School bus",
            serial_number=bus.bus_number if bus else None,
        )

    @property
    def bus(self) -> Bus | None:
        """The bus this entity belongs to, if it is still in the feed."""
        return (self.coordinator.data or {}).get(self._bus_id)

    @property
    def available(self) -> bool:
        """Whether the bus is still being reported."""
        return super().available and self.bus is not None


class GeoTrackStopEntity(GeoTrackBusEntity):
    """Base entity for one of your stops on a bus."""

    def __init__(
        self, coordinator: GeoTrackCoordinator, bus_id: int, stop_key: str
    ) -> None:
        """Initialise the entity."""
        super().__init__(coordinator, bus_id)
        self._stop_key = stop_key

    @property
    def stop(self) -> Stop | None:
        """The stop this entity tracks, if it is still in the feed."""
        bus = self.bus
        if bus is None:
            return None
        return next((s for s in bus.stops if s.key == self._stop_key), None)

    @property
    def available(self) -> bool:
        """Whether the stop is still being reported."""
        return super().available and self.stop is not None
