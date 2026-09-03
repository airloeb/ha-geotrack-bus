"""Constants for the GeoTrack Bus Tracking integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "geotrack_bus"

CONF_COOKIE: Final = "cookie"
CONF_HOST: Final = "host"
CONF_PHONE: Final = "phone"
CONF_COMMUNICATION: Final = "communication"

DEFAULT_HOST: Final = "parent.geotrackny.com"
DEFAULT_SCAN_INTERVAL: Final = 20
MIN_SCAN_INTERVAL: Final = 10
MAX_SCAN_INTERVAL: Final = 300

MANUFACTURER: Final = "GeoTrack"
