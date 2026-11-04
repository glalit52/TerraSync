"""A simulated ground radar, so the live-tracking path can be shown and tested.

Everything here is invented: the sector, the fence line, the movers. The sensor
is registered with `simulated=True`, which marks every track and alert it
produces, and the console says so on every screen those appear. The fence line
is a straight segment drawn for the demo; it is not a real boundary.

The scenario is a function of elapsed time and nothing else -- no randomness --
so a test can ask "where is the patrol vehicle at t=95" and get the same answer
every run, and a crossing that the tests expect at t~185 happens at t~185.

It repeats every CYCLE_S seconds with fresh track ids, so a demo left running
keeps producing events.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

#: Anchored on the Rann of Kutch demo area (IN-KCH-SECTOR), a salt flat.
ORIGIN = (70.512, 23.905)
CYCLE_S = 600
SENSOR_ID = "sim-radar-1"

FENCE_LAT = 23.925
FENCE = [[70.480, FENCE_LAT], [70.545, FENCE_LAT]]
STRIP = [[70.490, 23.927], [70.535, 23.927], [70.535, 23.934], [70.490, 23.934]]
CULVERT_C = (70.500, 23.918)
CULVERT = [[CULVERT_C[0] - 0.0015, CULVERT_C[1] - 0.0012], [CULVERT_C[0] + 0.0015, CULVERT_C[1] - 0.0012],
           [CULVERT_C[0] + 0.0015, CULVERT_C[1] + 0.0012], [CULVERT_C[0] - 0.0015, CULVERT_C[1] + 0.0012]]

ZONES = [
    {"name": "Demo fence line (simulated)", "rule": "line", "geometry": FENCE,
     "kinds": ["person", "vehicle", "drone"]},
    {"name": "Restricted strip (simulated)", "rule": "no_entry", "geometry": STRIP, "kinds": []},
    {"name": "Culvert (simulated)", "rule": "loiter", "geometry": CULVERT,
     "kinds": ["person", "vehicle"], "loiter_s": 60},
]

M_PER_DEG_LAT = 111_320.0


def _offset(p: tuple[float, float], east_m: float, north_m: float) -> tuple[float, float]:
    return (p[0] + east_m / (M_PER_DEG_LAT * math.cos(math.radians(p[1]))), p[1] + north_m / M_PER_DEG_LAT)


def _jitter(seed: float, t: float, metres: float = 4.0) -> tuple[float, float]:
    """Deterministic radar noise of a few metres."""
    return metres * math.sin(t * 1.7 + seed), metres * math.cos(t * 1.3 + seed * 2)


def _fix(uid: str, kind: str, p: tuple[float, float], t: float, seed: float) -> dict:
    e, n = _jitter(seed, t)
    lon, lat = _offset(p, e, n)
    return {"uid": uid, "kind": kind, "lon": round(lon, 7), "lat": round(lat, 7)}


def reports_at(elapsed_s: float) -> list[dict]:
    """Every mover the radar would report at this moment, without timestamps."""
    t = elapsed_s
    cycle = int(t // CYCLE_S)
    c = t % CYCLE_S
    out: list[dict] = []

    # 1. Patrol vehicle on the lateral road, 8 m/s, out and back. Never crosses.
    road_w, road_e, road_lat = 70.490, 70.535, 23.912
    span_m = (road_e - road_w) * M_PER_DEG_LAT * math.cos(math.radians(road_lat))
    s = (t * 8.0) % (2 * span_m)
    along = s if s <= span_m else 2 * span_m - s
    out.append(_fix("patrol-1", "vehicle", _offset((road_w, road_lat), along, 0), t, 1.0))

    # 2. Two people walking north at 1.5 m/s from 280 m south of the fence:
    #    cross it at ~187 s, enter the restricted strip at ~335 s, stop at
    #    ~23.929 and drop out of radar at 420 s.
    if 0 <= c < 420:
        for k, (dx, seed) in enumerate(((0.0, 2.0), (6.0, 3.0))):
            start = (70.515 + dx / 100_000, FENCE_LAT - 280 / M_PER_DEG_LAT)
            north = min(c * 1.5, 280 + 440)
            out.append(_fix(f"p{cycle}-{k + 1}", "person", _offset(start, dx, north), t, seed))

    # 3. A vehicle comes in from the east at 6 m/s, stops in the culvert for
    #    90 s (the loiter rule fires at 60 s), then leaves west.
    if 40 <= c < 330:
        arrive, dwell = 40 + 612 / 6.0, 90
        if c < arrive:
            p = _offset(CULVERT_C, 612 - (c - 40) * 6.0, 0)
        elif c < arrive + dwell:
            p = CULVERT_C
        else:
            p = _offset(CULVERT_C, -(c - arrive - dwell) * 6.0, 0)
        out.append(_fix(f"v{cycle}-1", "vehicle", p, t, 4.0))

    # 4. A small drone crossing the fence southward at 12 m/s.
    if 400 <= c < 480:
        start = (70.530, FENCE_LAT + 400 / M_PER_DEG_LAT)
        out.append(_fix(f"d{cycle}-1", "drone", _offset(start, 0, -(c - 400) * 12.0), t, 5.0))

    # 5. Clutter: an unknown return that lives for 20 s every two minutes.
    if c % 120 < 20:
        k = int(c // 120)
        spot = _offset(ORIGIN, -1500 + 700 * k, 900 + 150 * (k % 3))
        out.append(_fix(f"u{cycle}-{k}", "unknown", spot, t, 6.0 + k))
    return out


# ---------------------------------------------------------------------------
# Driving a running server
# ---------------------------------------------------------------------------

def _post(url: str, token: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        url.rstrip("/") + path, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{path}: HTTP {e.code} {e.read().decode(errors='replace')[:300]}") from e
    except urllib.error.URLError as e:
        raise SystemExit(f"cannot reach {url}: {e.reason}") from e


def _get(url: str, token: str, path: str) -> dict:
    req = urllib.request.Request(url.rstrip("/") + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"{}")


def setup(url: str, token: str) -> None:
    """Register the simulated radar and, if absent, the simulated zones."""
    _post(url, token, "/api/sensors", {
        "id": SENSOR_ID, "name": "Simulated ground radar", "kind": "simulator",
        "lon": ORIGIN[0], "lat": ORIGIN[1], "range_m": 6000, "simulated": True})
    have = {z["name"] for z in _get(url, token, "/api/zones").get("zones", [])}
    for z in ZONES:
        if z["name"] not in have:
            _post(url, token, "/api/zones", {**z, "simulated": True})


def run(url: str, token: str, interval_s: float = 2.0, duration_s: float = 0, quiet: bool = False) -> None:
    setup(url, token)
    started = time.monotonic()
    sent = alerts = 0
    while True:
        elapsed = time.monotonic() - started
        if duration_s and elapsed > duration_s:
            break
        now = datetime.now(timezone.utc).isoformat()
        reports = [{**r, "time": now} for r in reports_at(elapsed)]
        res = _post(url, token, "/api/tracks/ingest", {"sensor_id": SENSOR_ID, "reports": reports})
        sent += res.get("accepted", 0)
        for a in res.get("alerts", []):
            alerts += 1
            if not quiet:
                print(f"[{elapsed:6.0f}s] ALERT {a['summary']}")
        if not quiet and int(elapsed) % 30 < interval_s:
            print(f"[{elapsed:6.0f}s] {len(reports)} tracks reported, {sent} fixes, {alerts} alerts")
        time.sleep(max(0.0, interval_s - (time.monotonic() - started - elapsed)))
