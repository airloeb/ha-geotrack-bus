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

CONF_WARNING_MINUTES: Final = "warning_minutes"
DEFAULT_WARNING_MINUTES: Final = 5
MIN_WARNING_MINUTES: Final = 1
MAX_WARNING_MINUTES: Final = 30


MANUFACTURER: Final = "GeoTrack"
