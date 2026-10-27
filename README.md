# GeoTrack Bus Tracking for Home Assistant

Brings your school bus from the GeoTrack parent portal (`parent.geotrackny.com`)
into Home Assistant as a live map marker plus sensors you can build dashboards
and notifications on.

It can also sign in to the **BusRight** parent app instead — see
[BusRight](#busright). Add the integration once per service.

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
| `sensor.bus_123_stop_9_status` | `approaching` | `approaching` / `at_stop` / `passed` / `no_info` / `unknown` |
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

## The warning is threshold-driven, not learned

`binary_sensor.<stop>_arriving_soon` fires on fixed thresholds, so it works on a
route's **first** run rather than after days of calibration. Two signals,
whichever trips first:

- **Distance** — works on every route. Set under **Configure**, default
  0.75 miles, which measured about five minutes before arrival.
- **Stops away** — exact and portal-supplied, so it survives the feed freezing
  minutes before arrival. Default 1 stop. Only used where your stop is far
  enough down the route to have earlier stops to count: a rider at stop 1 reads
  zero for the whole approach, so the rule is skipped for them.

Which one fired is on the sensor's `triggered_by` attribute.

The two together cover the awkward cases. Near the front of a route there is no
progress runway and distance carries it; deep into a route the stop count fires
even while the bus is still miles away by road.

## The ETA sensor (informational)

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

The ETA sensor is informational: the warning itself no longer depends on it,
so a route with nothing learned still warns you on the thresholds above.

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

## BusRight

BusRight has no parent website, only its phone app, so this signs in the way
that app does. **Add Integration → GeoTrack Bus Tracking → BusRight app**,
enter the email on your BusRight parent account, then the code BusRight emails
you. Only a refresh token is stored; no password exists to store.

Next you choose routes. The list is every route in your children's schools;
the ones BusRight links to your child come pre-ticked. Schools often link a
child to the morning route but not the afternoon one, so tick any others your
child rides. For each route you then pick your stop (pre-selected where
BusRight knows it). **Configure** lets you change routes, stops and warning
thresholds later without signing in again.

### What it creates

One device per route:

| Entity | Notes |
| --- | --- |
| `device_tracker.busright_<route>` | live position from the driver's tablet |
| `sensor.busright_<route>_driver` | |
| `sensor.busright_<route>_last_report` | when the tablet last sent a position |
| `binary_sensor.busright_<route>_running` | BusRight says a run is in progress |
| `binary_sensor.busright_<route>_moving` | position changed since the last poll |

One device per stop of yours. A morning pick-up and an afternoon drop-off at
the same house share one stop device, which follows whichever run is on:

| Entity | Notes |
| --- | --- |
| `sensor.busright_stop_<street>_status` | `approaching` / `at_stop` / `passed` / `not_running` / `unknown` |
| `sensor.busright_stop_<street>_distance_to_stop` | straight-line |
| `sensor.busright_stop_<street>_stops_away` | stops the bus still has to make before yours |
| `sensor.busright_stop_<street>_eta` | minutes, from the route's planned leg times |
| `sensor.busright_stop_<street>_route_serving_this_stop` | |
| `binary_sensor.busright_stop_<street>_arriving_soon` | same thresholds as GeoTrack |
| `binary_sensor.busright_stop_<street>_bus_at_stop` | |
| `binary_sensor.busright_stop_<street>_already_passed` | resets when the next run starts |

The stop entities use the same names and states as GeoTrack's, so the same
automations work for either.

### How progress is worked out

BusRight publishes the route (its stops in order) and the bus's position, but
not how far along the route the bus is. That is tracked here, run by run:

- A stop counts as reached when the bus comes within 100 m of it. Progress
  only moves forward, a few stops at a time, so a later stop that happens to
  sit beside an earlier one cannot make the bus leap ahead.
- Your stop is **passed** once the bus has reached it, or come within 150 m,
  and then pulled 150 m further away.
- The **ETA** is BusRight's own planned time from the last stop reached to
  yours, less the time since. It is unknown until the bus reaches its first
  stop. Districts can hide planned times in the app; they are still in the
  route data.
- A new run is recognised by BusRight's session id for it, and everything
  resets then.
- A position older than 12 minutes is treated as stale, exactly as for
  GeoTrack: no distance, no warning.
- BusRight's position document can hold more than one coordinate pair. On
  the first live run a fixed point at the route's school stop rode along with
  the bus's own, and which came first varied from read to read. The bus is
  the pair that moves: a pair repeated exactly while another changes is
  remembered as a fixed place and ignored. A fix further from the last one
  than a bus could drive is also held back until the next few fixes bear it
  out.
- Until the bus reaches its first stop of a run, it can only be matched to
  stops up to yours, so one stray fix near a later stop cannot mark yours as
  passed before the bus has set off.
- The ETA never drops below the time needed to cover the straight-line
  distance at 30 mph, so a bus running behind the plan is not announced as
  "0 minutes away" while still a mile off.

The route is re-read every two minutes, which is also how soon a run starting
or ending is noticed. During a run the position is polled at the configured
interval.

**Not yet confirmed against a live run:** the position document's speed and
heading fields. They are passed through untouched as `reported_speed` and
`reported_heading` on the tracker, not presented as a speed sensor, because
their units are unknown. `moving` is worked out from the position instead.

**Finding BusRight's own ETA.** The app shows an ETA of its own, and where it
comes from is not yet known. While a run is on, every distinct position
document and the route's run-time fields are kept in memory (the last 600
changes), next to what this integration worked out at the time. **Settings →
Devices & Services → GeoTrack Bus Tracking → ⋮ → Download diagnostics** saves
them, along with any coordinate pairs being ignored as fixed places. Your email, sign-in token, driver names and stop notes are left out.

## Announcements and WhatsApp

`blueprints/automation/geotrack_bus/bus_alerts.yaml` announces on your
speakers and sends a WhatsApp message (through the WAHA integration) when a
stop's **Arriving soon** turns on, and optionally again at **Bus at stop**.
It works with GeoTrack and BusRight stops alike. Import it from
**Settings → Automations → Blueprints → Import Blueprint** with the file's
GitHub URL, then create one automation per child.

## Notes

- The integration is read-only; it never posts anything back to the portal.
- `stops_away` is derived from the portal's own status text
  (*"is before stop number 1, Your stop number is 9"*), so it is a stop count,
  not a time estimate. The portal does not publish an ETA.
- New buses and stops appear as entities automatically the first time they show
  up in the feed — no restart needed.
- A position is only used while it is fresh. The portal keeps handing back the
  last fix it ever saw for a vehicle, indefinitely — one was re-served unchanged
  for two and a half days — so a fix older than 12 minutes is discarded rather
  than measured from. When that happens the stop reports no distance and no ETA,
  the `position_stale` attribute goes `true`, and `arriving_soon` stays off. Use
  it in conditions if you notify on the raw status, which comes from the message
  text and can claim progress the position does not support.
- `no_info` is the portal saying outright that it has lost the bus
  (*"Sorry, No info for bus 123..."*). It is reported separately from `unknown`,
  which just means the message did not parse.
- `distance_from_named_bus` is `false` when the distance had to be taken from
  some other vehicle relaying the message, or when nothing confirms whose
  position it is. Treat the distance as indicative only in that case.

## License

MIT
