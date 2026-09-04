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


class GeoTrackStopEntity(CoordinatorEntity[GeoTrackCoordinator]):
    """Base entity for one of your stops.

    Deliberately not bound to a vehicle. The morning and afternoon runs are
    served by different buses, so a stop entity resolves whichever bus is
    currently reporting against it; binding to the bus seen at creation would
    make the stop go unavailable every time the vehicle changed.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: GeoTrackCoordinator, stop: Stop) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        self._stop_key = stop.key
        name = stop.label
        if name == "My stop" and stop.stop_number is not None:
            # The portal gives no address. The stop number it carried when
            # first seen makes a readable label, even though the number itself
            # drifts from run to run and is never used for identity.
            name = f"Stop {stop.stop_number}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"stop:{stop.key}")},
            manufacturer=MANUFACTURER,
            name=name,
            model="Bus stop",
        )

    @property
    def _pair(self) -> tuple[Bus, Stop] | None:
        """The bus currently serving this stop, and the stop itself."""
        for bus in (self.coordinator.data or {}).values():
            for stop in bus.stops:
                if stop.key == self._stop_key:
                    return bus, stop
        return None

    @property
    def bus(self) -> Bus | None:
        """Whichever bus is serving this stop right now."""
        pair = self._pair
        return pair[0] if pair else None

    @property
    def stop(self) -> Stop | None:
        """This stop as most recently reported."""
        pair = self._pair
        return pair[1] if pair else None

    @property
    def available(self) -> bool:
        """Whether any bus is currently reporting against this stop."""
        return super().available and self._pair is not None
