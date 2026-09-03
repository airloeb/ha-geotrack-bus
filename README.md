# GeoTrack Bus Tracking for Home Assistant

Brings your school bus from the GeoTrack parent portal (`parent.geotrackny.com`)
into Home Assistant as a live map marker plus sensors you can build dashboards
and notifications on.

## What it creates

One **device per bus**, with:

| Entity | Example | Notes |
| --- | --- | --- |
| `device_tracker.bus_399` | on the HA map | GPS position, updates while the bus moves |
| `sensor.bus_399_speed` | `34 mph` | |
| `sensor.bus_399_bearing` | `302°` | `direction` attribute gives `WNW` |
| `sensor.bus_399_current_address` | `16-1 Walker Dr, Lakewood, NJ` | reverse-geocoded by the portal |
| `sensor.bus_399_route` | `OBYHS1P` | |
| `sensor.bus_399_last_report` | timestamp | how fresh the position actually is |
| `binary_sensor.bus_399_moving` | `on` / `off` | |

Then, for each of **your** stops on that bus (one set per child/stop):

| Entity | Example | Notes |
| --- | --- | --- |
| `sensor.bus_399_stop_13_stops_away` | `12` | your stop number minus the bus's current stop |
| `sensor.bus_399_stop_13_status` | `approaching` | `approaching` / `at_stop` / `passed` / `unknown` |
| `sensor.bus_399_stop_13_distance` | `4.1 mi` | straight-line bus→stop distance |
| `sensor.bus_399_stop_13_message` | the portal's own wording | |
| `binary_sensor.bus_399_stop_13_bus_at_stop` | `on` / `off` | |
| `binary_sensor.bus_399_stop_13_already_passed` | `on` / `off` | resets when the portal starts a new run |

Entity ids depend on your bus and stop numbers — check **Settings → Devices** after setup.

## Install

1. Copy `custom_components/geotrack_bus` into your Home Assistant `config/custom_components/` folder
   (so you end up with `config/custom_components/geotrack_bus/manifest.json`).
2. Restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → GeoTrack Bus Tracking**.

Requires Home Assistant 2025.1 or newer.

## Getting the session cookie

The portal has no password — it signs you in with a code texted to your phone —
so there is nothing the integration can log in with on its own. Instead you hand
it the session your browser already has.

1. In a desktop browser, sign in at `https://parent.geotrackny.com/Home`.
2. Open DevTools (`⌘⌥I` / `F12`) → **Network** tab.
3. Reload the page and click the request named **`GetVehicles`**.
4. Under **Headers → Request Headers**, find **`Cookie`** and copy its entire value.
5. Paste that into the integration's *Session cookie* field. Pasting the whole
   `Cookie: ...` line is fine — the leading `Cookie:` is stripped for you.

Treat that string like a password; it *is* your login.

**When it expires:** Home Assistant raises a "reconfigure" notification. Sign in
again in your browser, grab a fresh cookie, and paste it into the re-auth prompt.
The portal sets a persistent device cookie, so in practice this is infrequent —
but it will happen eventually, and it will happen if you sign out in that browser.

## Polling

Defaults to every 20 seconds, matching the portal's own refresh. Change it under
the integration's **Configure** button (10–300 s). Positions only change while a
bus is on a run; off-shift the last known position is retained, and
`sensor.<bus>_last_report` tells you how old it really is.

## Dashboard

```yaml
type: vertical-stack
cards:
  - type: map
    entities:
      - device_tracker.bus_399
    hours_to_show: 1
    theme_mode: auto
  - type: entities
    title: Bus 399
    entities:
      - entity: sensor.bus_399_stop_13_status
        name: Status
      - entity: sensor.bus_399_stop_13_stops_away
        name: Stops away
      - entity: sensor.bus_399_stop_13_distance
        name: Distance to stop
      - entity: sensor.bus_399_speed
        name: Speed
      - entity: sensor.bus_399_current_address
        name: Now near
      - entity: sensor.bus_399_last_report
        name: Last report
```

## Automations

**Head outside — the bus is three stops away.** The `below` guard stops it
re-firing as the counter ticks down.

```yaml
alias: Bus 399 approaching
triggers:
  - trigger: numeric_state
    entity_id: sensor.bus_399_stop_13_stops_away
    below: 4
conditions:
  - condition: state
    entity_id: sensor.bus_399_stop_13_status
    state: approaching
actions:
  - action: notify.washing_machine_dryer
    data:
      title: School bus
      message: >-
        Bus 399 is {{ states('sensor.bus_399_stop_13_stops_away') }} stops away
        ({{ states('sensor.bus_399_stop_13_distance') }} mi).
mode: single
```

**The bus reached your stop.**

```yaml
alias: Bus 399 at our stop
triggers:
  - trigger: state
    entity_id: binary_sensor.bus_399_stop_13_bus_at_stop
    to: "on"
actions:
  - action: notify.washing_machine_dryer
    data:
      message: Bus 399 is at your stop now.
```

**Missed it.** Fires once when the portal flips the stop to "already passed".

```yaml
alias: Bus 399 already came
triggers:
  - trigger: state
    entity_id: binary_sensor.bus_399_stop_13_already_passed
    to: "on"
actions:
  - action: notify.washing_machine_dryer
    data:
      message: >-
        Bus 399 went by the stop at
        {{ state_attr('sensor.bus_399_stop_13_status', 'passed_at') }}.
```

## Notes

- The integration is read-only; it never posts anything back to the portal.
- `stops_away` is derived from the portal's own status text
  (*"is before stop number 1, Your stop number is 13"*), so it is a stop count,
  not a time estimate. The portal does not publish an ETA.
- New buses and stops appear as entities automatically the first time they show
  up in the feed — no restart needed.
