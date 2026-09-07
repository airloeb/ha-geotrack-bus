# GeoTrack Bus Tracking for Home Assistant

Brings your school bus from the GeoTrack parent portal (`parent.geotrackny.com`)
into Home Assistant as a live map marker plus sensors you can build dashboards
and notifications on.

> Entity ids below use a placeholder bus `123` and stop `9`. Yours will use your
> own bus and stop numbers — check **Settings → Devices & Services → Devices**
> after setup.

## What it creates

One **device per bus**, with:

| Entity | Example | Notes |
| --- | --- | --- |
| `device_tracker.bus_123` | on the HA map | GPS position, updates while the bus moves |
| `sensor.bus_123_speed` | `34 mph` | |
| `sensor.bus_123_bearing` | `302°` | `direction` attribute gives `WNW` |
| `sensor.bus_123_current_address` | `120 Main St, Springfield, NJ` | reverse-geocoded by the portal |
| `sensor.bus_123_route` | `ABC12P` | |
| `sensor.bus_123_last_report` | timestamp | how fresh the position actually is |
| `binary_sensor.bus_123_moving` | `on` / `off` | |

Then, for each of **your** stops on that bus (one set per child/stop):

| Entity | Example | Notes |
| --- | --- | --- |
| `sensor.bus_123_stop_9_stops_away` | `8` | your stop number minus the bus's current stop |
| `sensor.bus_123_stop_9_status` | `approaching` | `approaching` / `at_stop` / `passed` / `unknown` |
| `sensor.bus_123_stop_9_distance` | `4.1 mi` | straight-line bus→stop distance |
| `sensor.bus_123_stop_9_message` | the portal's own wording | |
| `sensor.bus_123_stop_9_eta` | `8 min` | learned from past runs; unknown until one completes |
| `binary_sensor.bus_123_stop_9_arriving_soon` | `on` / `off` | on when the learned ETA is inside your warning window |
| `sensor.bus_123_stop_9_runs_measured` | `3` | how many complete runs the estimate is based on |
| `binary_sensor.bus_123_stop_9_bus_at_stop` | `on` / `off` | |
| `binary_sensor.bus_123_stop_9_already_passed` | `on` / `off` | resets when the portal starts a new run |

## Install

### HACS (recommended)

1. **HACS → ⋮ → Custom repositories**, add this repository with type **Integration**.
2. Search HACS for **GeoTrack Bus Tracking** and download it.
3. Restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → GeoTrack Bus Tracking**.

### Manual

Copy `custom_components/geotrack_bus` into your Home Assistant
`config/custom_components/` folder, restart, then add the integration.

Requires Home Assistant 2025.1 or newer.

## Signing in

The portal has no password — it identifies you by phone number and a short code
it sends you. The integration offers two ways in.

### Phone code (recommended)

Pick **"Sign in with a code sent to my phone"**, enter the number registered with
your school's GeoTrack account, and choose text message or phone call. The portal
sends a code; type it into the next screen. Nothing else is needed, and it works
entirely from the Home Assistant app.

### Session cookie

Pick **"Paste a session cookie from my browser"** if you would rather not receive
a code, or if the phone step fails.

1. In a desktop browser, sign in at your portal's `/Home` page.
2. Open DevTools (`⌘⌥I` / `F12`) → **Network** tab.
3. Reload the page and click the request named **`GetVehicles`**.
4. Under **Headers → Request Headers**, copy the entire **`Cookie`** value.
5. Paste it in. Pasting the whole `Cookie: ...` line is fine — the leading
   `Cookie:` is stripped for you.

Treat that string like a password; it *is* your login.

**When the session expires:** Home Assistant raises a re-authentication
notification and offers both routes again. The portal sets a persistent device
cookie, so this is infrequent — but it will happen eventually, and it will happen
if you sign out in that browser.

## The ETA is learned, not assumed

The portal reports only which stop the bus is working on and where the vehicle
is. It has no arrival time to give, and this integration does not invent one
from an assumed pace.

Instead it watches. Through a run it records when the bus is seen in each 400 m
band of distance from your stop. When the bus reaches your stop, the portal
states the time it got there (*"was by your stop at 8:55 AM"*), and the gap back
to each of those sightings becomes that band's **lead time**. "The bus is 1.2
miles out" then means whatever 1.2 miles has actually meant on this route.

**Why distance and not stop number.** If your stop is number 1 on its route,
the portal only ever says *"is before stop number 1"* — the same string whether
the bus is five miles away or turning into your street. Stops-away is
structurally 0 and carries no information. Distance keeps resolving all the way
to the door, and works the same for any stop position.

Details:

- Lead times are stored **per route code**, so a morning route and an afternoon
  route never contaminate each other.
- One figure per run per band, so a bus idling in one band cannot outvote the
  bands either side of it. The last 20 runs are averaged.
- An unmeasured band is interpolated between its measured neighbours, but never
  extrapolated beyond the measured range — outside it, the ETA is unknown.
- Everything persists across restarts.

**When the portal never says the bus arrived.** Only about one run in six ends
with a "was by your stop at ..." message; far more often the feed simply stops
updating a few minutes before the bus reaches the stop. A run that tracked all
the way in and then went quiet is closed out using the **closest approach** as
the arrival, provided the bus got within half a mile — near enough that it
plainly served the stop. A run that never got close is discarded rather than
guessed at.

An inferred arrival is a few minutes earlier than the real one, so estimates
built on it run slightly conservative. The ETA sensor's `last_arrival`
attribute reads `reported` or `inferred` so you can tell which you are looking
at. A reported arrival always wins when the portal offers one.

**Cold start: there is no ETA until a route has been watched through one
complete run.** `sensor.<bus>_<stop>_eta` stays unknown and
`binary_sensor.<bus>_<stop>_arriving_soon` stays off, so no warning fires on day
one. `sensor.<bus>_<stop>_runs_measured` shows how many runs are banked, and the
`warning_at_miles` attribute says how far out the warning will fire.

`arriving_soon` turns on when the learned ETA falls inside the window set under
**Configure** (default 5 minutes) — that is the entity to hang a "leave the
house now" notification on.

It is still an estimate: it assumes today's run resembles recent ones, and knows
nothing about traffic or an unusually long boarding.

## One message, many stops

Each Response the portal returns carries a line for *every* stop on the account,
and vehicles return Responses for stops served by other buses. So a single
Message may read:

```
bus 123, Route: ABC12P is before stop number 1, Your stop number is 1
bus 456, Route: XYZ34P is before stop number 1, Your stop number is 3
```

The integration picks the line whose *"Your stop number is N"* matches the
Response, and drops the Response entirely when the line names a different bus.
Without that, stops inherit the wrong route code and phantom stop entities
appear under buses that do not serve them.

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
      - device_tracker.bus_123
    hours_to_show: 1
    theme_mode: auto
  - type: entities
    title: Bus 123
    entities:
      - entity: sensor.bus_123_stop_9_status
        name: Status
      - entity: sensor.bus_123_stop_9_stops_away
        name: Stops away
      - entity: sensor.bus_123_stop_9_distance
        name: Distance to stop
      - entity: sensor.bus_123_speed
        name: Speed
      - entity: sensor.bus_123_current_address
        name: Now near
      - entity: sensor.bus_123_last_report
        name: Last report
```

## Automations

**Head outside — the bus is a few stops away.** The `below` guard stops it
re-firing as the counter ticks down.

```yaml
alias: Bus approaching
triggers:
  - trigger: numeric_state
    entity_id: sensor.bus_123_stop_9_stops_away
    below: 4
conditions:
  - condition: state
    entity_id: sensor.bus_123_stop_9_status
    state: approaching
actions:
  - action: notify.mobile_app_your_phone
    data:
      title: School bus
      message: >-
        Bus 123 is {{ states('sensor.bus_123_stop_9_stops_away') }} stops away
        ({{ states('sensor.bus_123_stop_9_distance') }} mi).
mode: single
```

**The bus reached your stop.**

```yaml
alias: Bus at our stop
triggers:
  - trigger: state
    entity_id: binary_sensor.bus_123_stop_9_bus_at_stop
    to: "on"
actions:
  - action: notify.mobile_app_your_phone
    data:
      message: Bus 123 is at your stop now.
```

**Missed it.** Fires once when the portal flips the stop to "already passed".

```yaml
alias: Bus already came
triggers:
  - trigger: state
    entity_id: binary_sensor.bus_123_stop_9_already_passed
    to: "on"
actions:
  - action: notify.mobile_app_your_phone
    data:
      message: >-
        Bus 123 went by the stop at
        {{ state_attr('sensor.bus_123_stop_9_status', 'passed_at') }}.
```

## Notes

- The integration is read-only; it never posts anything back to the portal.
- `stops_away` is derived from the portal's own status text
  (*"is before stop number 1, Your stop number is 9"*), so it is a stop count,
  not a time estimate. The portal does not publish an ETA.
- New buses and stops appear as entities automatically the first time they show
  up in the feed — no restart needed.

## License

MIT
