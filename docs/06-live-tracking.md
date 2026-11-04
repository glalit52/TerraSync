# Live tracking, phase 1

Satellites count what is on the ground on each pass; they cannot follow it. A
person is one or two pixels at the best commercial resolution, and the next
pass is hours or days away. Following people and vehicles needs sensors that
keep looking — ground surveillance radar, camera towers, unattended ground
sensors — and a layer that turns their reports into tracks, checks them against
geofences, and puts an alert in front of an operator in seconds.

Phase 1 is that layer, for tracks a sensor has already formed. It is
`src/terrashield/tracking.py`, eleven API routes, and the console's **Live
tracks** screen.

## What phase 1 does

| | |
|---|---|
| Intake | JSON reports and Cursor-on-Target events, in batches per sensor, at `POST /api/tracks/ingest` |
| Tracks | One per sensor and sensor track id. Speed and heading derived from consecutive fixes when the sensor does not send them. Lost after 30 s silent |
| Geofences | **Line** (crossing, debounced 30 s), **no entry** (once per entry, with 30 s hysteresis at the edge), **loiter** (inside longer than a limit). Each can be limited to kinds |
| Alerts | Raised within the ingest call, with a copy of the trail that raised them. Confirm, reject or escalate, audited |
| Live screen | Tracks and trails, zones, sensor coverage, an alert queue reviewed in place, zone drawing, sensor status, replay of stored fixes |
| Retention | Raw fixes and tracks purged after `TERRASHIELD_TRACK_RETENTION_HOURS` (default 72). Alerts keep their trail |
| Simulator | `terrashield simulate-tracks` — a scripted radar over the Rann of Kutch demo area, flagged simulated everywhere |

## What it does not do

Stated so a screen is not read as more than it is.

- **No fusion.** One mover seen by two sensors is two tracks. Associating them is phase 3.
- **No video or radar processing.** Tracks arrive formed. Detection on camera video at the tower and radar-to-camera cueing are phase 2. ASTERIX (the binary radar format) needs a converter to the JSON or CoT form.
- **No identity.** A track is a sensor's identifier, a kind and positions. There is no column for who or whose. CoT's affiliation field (friend, hostile, neutral, unknown) is dropped on intake: it is a judgement made upstream that this system has no way to check, and it would put the vocabulary the imagery schema refuses straight back on the map.
- **No streaming push.** The console polls every two seconds, which matches a radar sweep. Server-sent events or WebSockets are a backend change for when sensor counts make polling wasteful.
- **Single process.** Intake runs in the API process against SQLite. Kafka and a separate tracker service are the production shape when sensor counts grow.

## Running it

```bash
TERRASHIELD_TOKEN=devtoken terrashield --db terrashield.db --org <org-id> serve
```

```bash
terrashield simulate-tracks --url http://127.0.0.1:8787 --token devtoken
```

Open `/console/live`. Within the first ten-minute cycle the simulator produces
seven alerts: two people crossing the demo fence line, the same two entering the
restricted strip, a drone first seen over the strip and then crossing the line,
and a vehicle loitering at the culvert past its 60 s limit. The patrol vehicle on
the lateral road and the radar clutter raise nothing. `tests/test_ts_tracking.py`
pins exactly that.

The fence line, the strip and the culvert are invented shapes for the demo. They
are not a real boundary, and the console says so whenever a simulated sensor is
connected.

## Connecting a real sensor

1. Create a service token with the analyst role for the gateway.
2. Register the sensor once:

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"id":"radar-7","name":"Post 7 GSR","kind":"radar","lon":70.51,"lat":23.90,"range_m":6000}' \
  https://<host>/api/sensors
```

3. Post each sweep as a batch. JSON:

```json
{"sensor_id": "radar-7",
 "reports": [{"uid": "T042", "kind": "person", "lon": 70.51, "lat": 23.92,
              "time": "2026-11-04T02:14:07Z", "speed_mps": 1.4, "heading_deg": 12}]}
```

or CoT, as strings in a `cot` array. The CoT type's dimension sets the kind
(`a-?-G-U-C-I` person, `a-?-G-E-V` vehicle, `a-?-A` drone); a
`<detail><terrashield kind="person"/></detail>` element overrides it.

A malformed report refuses the whole batch and names the report. A sensor whose
fixes were being dropped silently would look, on the map, exactly like a quiet
sector. Reports more than 60 s in the future, or older than the retention window,
are refused individually with the reason.

## Permissions and audit

| Permission | Role | Covers |
|---|---|---|
| `track.read` | viewer | live screen, zones, sensors, alerts |
| `track.ingest` | analyst | posting reports (the gateway's service token) |
| `zone.manage` | analyst | creating and retiring zones |
| `sensor.manage` | analyst | registering sensors |
| `finding.review` | analyst | confirming or rejecting an alert |
| `finding.escalate` | supervisor | escalating an alert |

Audited: sensor registration, zone changes, every alert review, every read of a
track's full history, every replay. Not audited per fix: a radar at one sweep a
second would put eighty thousand entries a day into the hash chain. Each sensor
keeps a running count of fixes received instead.

## Before this watches a real border

- **Legal authority.** Deploy only for an agency with the mandate to watch that
  zone, and keep geofences inside it.
- **Retention.** Set `TERRASHIELD_TRACK_RETENTION_HOURS` to the agency's policy,
  not the default.
- **Measure false alarms.** Radar clutter, animals and weather produce tracks. Run
  the zones in shadow for a period and count alerts an operator rejects before
  wiring them to anything that pages a person.
