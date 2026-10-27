"""Diagnostics for GeoTrack Bus Tracking."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import (
    CONF_COOKIE,
    CONF_EMAIL,
    CONF_PHONE,
    CONF_PROVIDER,
    CONF_REFRESH_TOKEN,
    PROVIDER_BUSRIGHT,
)
from .coordinator import GeoTrackConfigEntry

TO_REDACT = {CONF_COOKIE, CONF_EMAIL, CONF_PHONE, CONF_REFRESH_TOKEN}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: GeoTrackConfigEntry
) -> dict[str, Any]:
    """Return the entry's settings and, for BusRight, what it has been sent."""
    out: dict[str, Any] = {
        "data": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
    }
    coordinator = getattr(entry, "runtime_data", None)
    if entry.data.get(CONF_PROVIDER) != PROVIDER_BUSRIGHT or coordinator is None:
        return out

    out["routes"] = [
        {
            "route": status.watched.name,
            "running": status.running,
            "status": status.status,
            "eta_minutes": status.eta_minutes,
            "stops_away": status.stops_away,
            "distance_m": status.distance_m,
            "your_stop_number": None if status.my_index is None else status.my_index + 1,
            "last_stop_reached": status.last_reached,
            "ignored_fixed_places": sorted(
                coordinator.fixed_points.get(status.watched.route_id, set())
            ),
            "stops": [
                {
                    "number": stop.index + 1,
                    "label": stop.label,
                    "planned_seconds_from_start": stop.scheduled_offset,
                }
                for stop in (status.route.stops if status.route else [])
            ],
        }
        for status in coordinator.statuses.values()
    ]
    out["capture"] = list(coordinator.capture)
    return out
