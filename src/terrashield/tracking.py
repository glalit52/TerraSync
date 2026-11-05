"""Live tracking, phase 1: sensor tracks, geofences, alerts.

Satellites revisit in days, so they count what is on the ground and cannot
follow it. Following something moving needs a sensor that keeps looking: a
ground surveillance radar, a camera tower, a gateway already publishing tracks
in Cursor-on-Target. This module is where their reports land.

What phase 1 does and does not do, stated so nobody reads more into it:

* It takes tracks that a sensor has already formed. A radar's tracker assigns
  an identifier to each mover; that identifier, per sensor, is a track here.
  Fusing one mover seen by two sensors into one track is phase 3 and is not
  attempted -- two sensors, two tracks.
* Geofences raise alerts in seconds: crossing a line, entering a no-entry
  zone (once per entry, not once per radar sweep), loitering in a zone longer
  than its limit. A human confirms or rejects every one.
* Tracks are anonymous. A track is a kind (person, vehicle, drone, unknown)
  and positions. CoT carries an affiliation -- friend, hostile, neutral -- and
  it is dropped on intake: it is a judgement made upstream that this system
  has no way to check, and the same boundary that keeps intent out of the
  imagery schema keeps it out of this one.
* Raw positions are purged after a retention window. An alert keeps a copy of
  the trail that raised it, so the evidence for a decision outlives the purge.

Reports are audited by batch on the sensor record, not per point -- a radar at
one sweep per second would put eighty thousand entries a day into the hash
chain. Reads of track history and replays are audited individually, because in
a surveillance system the sensitive operation is the read.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .geo import contains, haversine_m, validate_ring

KINDS = ("person", "vehicle", "drone", "unknown")
SENSOR_KINDS = ("radar", "camera", "ground_sensor", "cot_gateway", "simulator", "other")
RULES = ("line", "no_entry", "loiter")

#: A track with no report for this long is shown as lost rather than live.
LOST_AFTER_S = 30
#: Reports this far in the future are refused: a clock that wrong poisons ordering.
MAX_CLOCK_SKEW_S = 60
#: Repeated crossings of the same line by the same track inside this window are
#: one event, and leaving and re-entering a zone inside it is one presence.
#: Radar jitter at a boundary otherwise reads as a dozen intrusions.
LINE_DEBOUNCE_S = 30
ZONE_DEBOUNCE_S = 30
MAX_BATCH = 2000
MAX_COT_BYTES = 16_384


class TrackingError(ValueError):
    """A report or a zone that cannot be accepted, with the reason."""


def retention_hours() -> float:
    try:
        return max(1.0, float(os.environ.get("TERRASHIELD_TRACK_RETENTION_HOURS", "72")))
    except ValueError:
        return 72.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _parse_time(raw: Any) -> datetime:
    if raw is None or raw == "":
        return _now()
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        t = datetime.fromisoformat(text)
    except ValueError as e:
        raise TrackingError(f"time {raw!r} is not ISO 8601 or epoch seconds") from e
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Report:
    uid: str
    kind: str
    lon: float
    lat: float
    t: datetime
    speed_mps: float | None = None
    heading_deg: float | None = None


def _num(v: Any, name: str) -> float | None:
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError) as e:
        raise TrackingError(f"{name} must be a number, got {v!r}") from e
    if not math.isfinite(x):
        raise TrackingError(f"{name} must be finite")
    return x


def _validated(uid: str, kind: str, lon: float | None, lat: float | None, t: datetime,
               speed: float | None, heading: float | None) -> Report:
    if not uid:
        raise TrackingError("each report needs a track id (uid)")
    if lon is None or lat is None or not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise TrackingError(f"track {uid}: position {lon}, {lat} is not a coordinate")
    if kind not in KINDS:
        kind = "unknown"
    if heading is not None:
        heading %= 360
    if speed is not None and speed < 0:
        speed = None
    return Report(uid=uid[:128], kind=kind, lon=lon, lat=lat, t=t, speed_mps=speed, heading_deg=heading)


def parse_json_report(d: dict) -> Report:
    """The generic form: {uid, kind, lon, lat, time, speed_mps?, heading_deg?}."""
    if not isinstance(d, dict):
        raise TrackingError("a report must be a JSON object")
    return _validated(
        str(d.get("uid") or d.get("track_id") or ""),
        str(d.get("kind", "unknown")).lower(),
        _num(d.get("lon"), "lon"), _num(d.get("lat"), "lat"),
        _parse_time(d.get("time")),
        _num(d.get("speed_mps"), "speed_mps"), _num(d.get("heading_deg"), "heading_deg"))


def _kind_from_cot_type(cot_type: str) -> str:
    """Dimension only. Position 2 of a CoT atom type is affiliation and is ignored.

    a-?-G-U-C-I (ground unit, infantry) -> person; a-?-G-E-V (ground
    equipment, vehicle) -> vehicle; a-?-A (air) -> drone, since a ground radar
    or camera on a border is not reporting airliners.
    """
    parts = cot_type.split("-")
    if len(parts) < 3 or parts[0] != "a":
        return "unknown"
    dim = parts[2:]
    if dim[0] == "A":
        return "drone"
    if dim[0] == "G":
        if dim[1:3] == ["E", "V"]:
            return "vehicle"
        if dim[1:2] == ["U"] and "I" in dim[2:]:
            return "person"
    return "unknown"


def parse_cot(xml_text: str) -> Report:
    """One CoT <event>. Affiliation, callsigns and remarks are not read.

    ElementTree does not resolve external entities, and the bundled expat
    refuses entity-expansion bombs; the size cap bounds the rest.
    """
    if len(xml_text.encode()) > MAX_COT_BYTES:
        raise TrackingError(f"CoT message larger than {MAX_COT_BYTES} bytes")
    try:
        ev = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise TrackingError(f"CoT is not well-formed XML: {e}") from e
    if ev.tag != "event":
        raise TrackingError("CoT root element must be <event>")
    point = ev.find("point")
    if point is None:
        raise TrackingError("CoT event has no <point>")
    kind = _kind_from_cot_type(ev.get("type", ""))
    override = ev.find("detail/terrashield")
    if override is not None and override.get("kind") in KINDS:
        kind = override.get("kind")
    track = ev.find("detail/track")
    return _validated(
        ev.get("uid", ""), kind,
        _num(point.get("lon"), "lon"), _num(point.get("lat"), "lat"),
        _parse_time(ev.get("time")),
        _num(track.get("speed"), "speed") if track is not None else None,
        _num(track.get("course"), "course") if track is not None else None)


# ---------------------------------------------------------------------------
# Geometry for geofences
# ---------------------------------------------------------------------------

Point = tuple[float, float]


def _orient(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_cross(p1: Point, p2: Point, q1: Point, q2: Point) -> bool:
    """Proper or touching intersection of two segments, in lon/lat."""
    d1, d2 = _orient(q1, q2, p1), _orient(q1, q2, p2)
    d3, d4 = _orient(p1, p2, q1), _orient(p1, p2, q2)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 and d2 and d3 and d4:
        return True

    def on(a: Point, b: Point, c: Point) -> bool:
        return (min(a[0], b[0]) <= c[0] <= max(a[0], b[0])
                and min(a[1], b[1]) <= c[1] <= max(a[1], b[1]))
    return ((d1 == 0 and on(q1, q2, p1)) or (d2 == 0 and on(q1, q2, p2))
            or (d3 == 0 and on(p1, p2, q1)) or (d4 == 0 and on(p1, p2, q2)))


def crosses_line(prev: Point, cur: Point, line: list[Point]) -> bool:
    return any(segments_cross(prev, cur, line[i - 1], line[i]) for i in range(1, len(line)))


def _bearing(a: Point, b: Point) -> float:
    lat1, lat2 = math.radians(a[1]), math.radians(b[1])
    dlon = math.radians(b[0] - a[0])
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


# ---------------------------------------------------------------------------
# Zones
# ---------------------------------------------------------------------------

@dataclass
class Zone:
    id: str
    name: str
    rule: str
    geometry: list[Point]
    kinds: list[str] = field(default_factory=list)
    loiter_s: int = 0
    simulated: bool = False
    active: bool = True

    def applies_to(self, kind: str) -> bool:
        return not self.kinds or kind in self.kinds

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "rule": self.rule,
                "geometry": [list(p) for p in self.geometry], "kinds": self.kinds,
                "loiter_s": self.loiter_s, "simulated": self.simulated,
                "active": self.active}


def validate_zone(name: str, rule: str, geometry: list, kinds: list, loiter_s: int) -> list[Point]:
    if not name.strip():
        raise TrackingError("a zone needs a name")
    if rule not in RULES:
        raise TrackingError(f"rule must be one of: {', '.join(RULES)}")
    try:
        pts = [(float(x), float(y)) for x, y in geometry]
    except (TypeError, ValueError) as e:
        raise TrackingError("geometry must be a list of [lon, lat] pairs") from e
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        raise TrackingError(f"unknown kinds {bad}; use {', '.join(KINDS)}")
    if rule == "line":
        if len(pts) < 2:
            raise TrackingError("a line needs at least two points")
    else:
        if pts and pts[0] == pts[-1]:
            pts = pts[:-1]
        problems = validate_ring(pts)
        if problems:
            raise TrackingError("zone polygon: " + "; ".join(problems))
    if rule == "loiter" and loiter_s < 10:
        raise TrackingError("a loiter rule needs a limit of at least 10 seconds")
    return pts


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

def _track_id(org: str, sensor: str, uid: str) -> str:
    return "trk-" + hashlib.sha256(f"{org}|{sensor}|{uid}".encode()).hexdigest()[:14]


class Tracking:
    """Tracks, zones and alerts for one organisation, over a `Store`'s connection."""

    def __init__(self, store) -> None:
        self.store = store
        self.conn = store.conn
        self.org = store._org()

    # -- sensors -----------------------------------------------------------

    def register_sensor(self, name: str, kind: str, lon: float | None = None,
                        lat: float | None = None, range_m: float = 0,
                        simulated: bool = False, sensor_id: str = "") -> dict:
        self.store._require("sensor.manage")
        if kind not in SENSOR_KINDS:
            raise TrackingError(f"sensor kind must be one of: {', '.join(SENSOR_KINDS)}")
        if not name.strip():
            raise TrackingError("a sensor needs a name")
        sid = sensor_id or f"sen-{uuid.uuid4().hex[:10]}"
        existing = self.conn.execute("SELECT org_id FROM sensors WHERE id = ?", (sid,)).fetchone()
        if existing and existing["org_id"] != self.org:
            raise TrackingError(f"sensor id {sid} is taken")
        with self.conn:
            self.conn.execute(
                "INSERT INTO sensors (id, org_id, name, kind, lon, lat, range_m, simulated, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
                " kind=excluded.kind, lon=excluded.lon, lat=excluded.lat, range_m=excluded.range_m,"
                " simulated=excluded.simulated",
                (sid, self.org, name.strip(), kind, lon, lat, float(range_m or 0), int(simulated), _iso(_now())))
        self.store.audit("sensor.register", sid, {"name": name, "kind": kind, "simulated": simulated})
        return self.sensor(sid)

    def sensor(self, sid: str) -> dict:
        row = self.conn.execute("SELECT * FROM sensors WHERE id = ? AND org_id = ?", (sid, self.org)).fetchone()
        if row is None:
            raise TrackingError(f"no sensor {sid}")
        return self._sensor_dict(row)

    def _sensor_dict(self, r) -> dict:
        heard = r["last_heard_at"]
        age = (_now() - datetime.fromisoformat(heard)).total_seconds() if heard else None
        return {"id": r["id"], "name": r["name"], "kind": r["kind"], "lon": r["lon"], "lat": r["lat"],
                "range_m": r["range_m"], "simulated": bool(r["simulated"]), "reports": r["reports"],
                "last_heard_at": heard or None,
                "status": "never heard" if age is None else ("online" if age <= LOST_AFTER_S else "silent")}

    def sensors(self) -> list[dict]:
        self.store._require("track.read")
        rows = self.conn.execute("SELECT * FROM sensors WHERE org_id = ? ORDER BY name", (self.org,)).fetchall()
        return [self._sensor_dict(r) for r in rows]

    # -- zones -------------------------------------------------------------

    def put_zone(self, name: str, rule: str, geometry: list, kinds: list | None = None,
                 loiter_s: int = 0, simulated: bool = False) -> Zone:
        self.store._require("zone.manage")
        kinds = list(kinds or [])
        pts = validate_zone(name, rule, geometry, kinds, int(loiter_s or 0))
        z = Zone(id=f"zone-{uuid.uuid4().hex[:10]}", name=name.strip(), rule=rule, geometry=pts,
                 kinds=kinds, loiter_s=int(loiter_s or 0), simulated=simulated)
        with self.conn:
            self.conn.execute(
                "INSERT INTO zones (id, org_id, name, rule, geometry_json, kinds_json, loiter_s,"
                " simulated, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (z.id, self.org, z.name, rule, json.dumps(pts), json.dumps(kinds), z.loiter_s,
                 int(simulated), self.store.actor, _iso(_now())))
        self.store.audit("zone.create", z.id, {"name": z.name, "rule": rule})
        return z

    def retire_zone(self, zone_id: str) -> bool:
        self.store._require("zone.manage")
        with self.conn:
            cur = self.conn.execute("UPDATE zones SET active = 0 WHERE id = ? AND org_id = ? AND active = 1",
                                    (zone_id, self.org))
            self.conn.execute("DELETE FROM zone_presence WHERE zone_id = ?", (zone_id,))
        if cur.rowcount:
            self.store.audit("zone.retire", zone_id)
        return bool(cur.rowcount)

    def zones(self, active_only: bool = True) -> list[Zone]:
        sql = "SELECT * FROM zones WHERE org_id = ?" + (" AND active = 1" if active_only else "")
        return [Zone(id=r["id"], name=r["name"], rule=r["rule"],
                     geometry=[tuple(p) for p in json.loads(r["geometry_json"])],
                     kinds=json.loads(r["kinds_json"]), loiter_s=r["loiter_s"],
                     simulated=bool(r["simulated"]), active=bool(r["active"]))
                for r in self.conn.execute(sql + " ORDER BY created_at", (self.org,)).fetchall()]

    # -- intake ------------------------------------------------------------

    def ingest(self, sensor_id: str, reports: list[Report], now: datetime | None = None) -> dict:
        """Apply a batch of reports from one sensor. Returns counts and new alerts."""
        self.store._require("track.ingest")
        now = now or _now()
        sensor = self.conn.execute("SELECT * FROM sensors WHERE id = ? AND org_id = ?",
                                   (sensor_id, self.org)).fetchone()
        if sensor is None:
            raise TrackingError(f"no sensor {sensor_id}; register it first")
        if len(reports) > MAX_BATCH:
            raise TrackingError(f"at most {MAX_BATCH} reports per batch")
        simulated = bool(sensor["simulated"])
        zones = self.zones()
        cutoff = now - timedelta(hours=retention_hours())
        accepted, refused, alerts = 0, [], []

        with self.conn:
            for rep in sorted(reports, key=lambda r: r.t):
                if rep.t > now + timedelta(seconds=MAX_CLOCK_SKEW_S):
                    refused.append(f"{rep.uid}: time {rep.t.isoformat()} is in the future")
                    continue
                if rep.t < cutoff:
                    refused.append(f"{rep.uid}: older than the retention window")
                    continue
                alerts.extend(self._apply(sensor_id, rep, zones, simulated))
                accepted += 1
            self.conn.execute(
                "UPDATE sensors SET last_heard_at = ?, reports = reports + ? WHERE id = ?",
                (_iso(now), accepted, sensor_id))
            self._purge(cutoff)
        return {"accepted": accepted, "refused": refused, "alerts": alerts}

    def _apply(self, sensor_id: str, rep: Report, zones: list[Zone], simulated: bool) -> list[dict]:
        tid = _track_id(self.org, sensor_id, rep.uid)
        row = self.conn.execute("SELECT * FROM tracks WHERE id = ?", (tid,)).fetchone()
        t = _iso(rep.t)
        speed, heading = rep.speed_mps, rep.heading_deg
        prev: Point | None = None
        in_order = True
        if row is not None:
            prev = (row["lon"], row["lat"])
            prev_t = datetime.fromisoformat(row["last_seen"])
            in_order = rep.t > prev_t
            dt = (rep.t - prev_t).total_seconds()
            # Derive what the sensor did not send, from consecutive fixes.
            if in_order and dt > 0:
                if speed is None:
                    speed = haversine_m(prev, (rep.lon, rep.lat)) / dt
                if heading is None and prev != (rep.lon, rep.lat):
                    heading = _bearing(prev, (rep.lon, rep.lat))
        if row is None:
            self.conn.execute(
                "INSERT INTO tracks (id, org_id, sensor_id, uid, kind, first_seen, last_seen, lon, lat,"
                " speed_mps, heading_deg, points, simulated) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?)",
                (tid, self.org, sensor_id, rep.uid, rep.kind, t, t, rep.lon, rep.lat, speed, heading, int(simulated)))
        # Every fix is kept as history, in order or not.
        self.conn.execute(
            "INSERT INTO track_points (org_id, track_id, t, lon, lat, speed_mps, heading_deg)"
            " VALUES (?,?,?,?,?,?,?)", (self.org, tid, t, rep.lon, rep.lat, speed, heading))
        if row is None:
            pass
        elif in_order:
            kind = rep.kind if rep.kind != "unknown" else row["kind"]
            self.conn.execute(
                "UPDATE tracks SET last_seen=?, lon=?, lat=?, speed_mps=?, heading_deg=?, kind=?,"
                " points = points + 1 WHERE id = ?", (t, rep.lon, rep.lat, speed, heading, kind, tid))
        else:
            # A late fix is history, not the present: stored, counted, and kept
            # out of the geofence logic, which needs positions in order.
            self.conn.execute("UPDATE tracks SET points = points + 1 WHERE id = ?", (tid,))
            return []

        kind = rep.kind if (row is None or rep.kind != "unknown") else row["kind"]
        cur: Point = (rep.lon, rep.lat)
        out = []
        for z in zones:
            if not z.applies_to(kind):
                continue
            if z.rule == "line":
                if prev is not None and crosses_line(prev, cur, z.geometry):
                    recent = self.conn.execute(
                        "SELECT at FROM track_alerts WHERE track_id = ? AND zone_id = ? ORDER BY seq DESC LIMIT 1",
                        (tid, z.id)).fetchone()
                    if recent is None or (rep.t - datetime.fromisoformat(recent["at"])).total_seconds() > LINE_DEBOUNCE_S:
                        out.append(self._alert(z, tid, sensor_id, kind, rep, simulated,
                                               f"{kind} track crossed {z.name}"))
                continue
            inside = contains(z.geometry, cur)
            key = (z.id, tid)
            pres = self.conn.execute("SELECT * FROM zone_presence WHERE zone_id = ? AND track_id = ?", key).fetchone()
            if not inside:
                if pres is not None and not pres["left_at"]:
                    self.conn.execute("UPDATE zone_presence SET left_at = ? WHERE zone_id = ? AND track_id = ?", (t, *key))
                continue
            if pres is not None and pres["left_at"]:
                away = (rep.t - datetime.fromisoformat(pres["left_at"])).total_seconds()
                if away <= ZONE_DEBOUNCE_S:
                    # Back inside before the debounce ran out: the same presence.
                    self.conn.execute("UPDATE zone_presence SET left_at = '' WHERE zone_id = ? AND track_id = ?", key)
                    pres = self.conn.execute("SELECT * FROM zone_presence WHERE zone_id = ? AND track_id = ?", key).fetchone()
                else:
                    self.conn.execute("DELETE FROM zone_presence WHERE zone_id = ? AND track_id = ?", key)
                    pres = None
            if pres is None:
                self.conn.execute("INSERT INTO zone_presence (org_id, zone_id, track_id, entered_at) VALUES (?,?,?,?)",
                                  (self.org, z.id, tid, t))
                if z.rule == "no_entry":
                    self.conn.execute("UPDATE zone_presence SET alerted = 1 WHERE zone_id = ? AND track_id = ?", key)
                    out.append(self._alert(z, tid, sensor_id, kind, rep, simulated, f"{kind} track entered {z.name}"))
            elif z.rule == "loiter" and not pres["alerted"]:
                dwell = (rep.t - datetime.fromisoformat(pres["entered_at"])).total_seconds()
                if dwell >= z.loiter_s:
                    self.conn.execute("UPDATE zone_presence SET alerted = 1 WHERE zone_id = ? AND track_id = ?", (z.id, tid))
                    out.append(self._alert(z, tid, sensor_id, kind, rep, simulated,
                                           f"{kind} track in {z.name} for {int(dwell)} s (limit {z.loiter_s} s)"))
        return out

    def _alert(self, z: Zone, tid: str, sensor_id: str, kind: str, rep: Report,
               simulated: bool, summary: str) -> dict:
        trail = [[r["lon"], r["lat"], r["t"]] for r in self.conn.execute(
            "SELECT lon, lat, t FROM track_points WHERE track_id = ? ORDER BY t DESC LIMIT 120", (tid,)).fetchall()][::-1]
        aid = f"talert-{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            "INSERT INTO track_alerts (id, org_id, zone_id, zone_name, track_id, sensor_id, rule, kind, at,"
            " lon, lat, summary, trail_json, simulated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, self.org, z.id, z.name, tid, sensor_id, z.rule, kind, _iso(rep.t), rep.lon, rep.lat,
             summary, json.dumps(trail), int(simulated)))
        return {"id": aid, "zone": z.name, "rule": z.rule, "track_id": tid, "summary": summary}

    def _purge(self, cutoff: datetime) -> None:
        c = _iso(cutoff)
        self.conn.execute("DELETE FROM track_points WHERE org_id = ? AND t < ?", (self.org, c))
        self.conn.execute("DELETE FROM tracks WHERE org_id = ? AND last_seen < ?", (self.org, c))

    # -- reads -------------------------------------------------------------

    def live(self, since_alert: int = 0, window_s: int = 600, trail: int = 40,
             now: datetime | None = None) -> dict:
        """Everything the live screen draws, in one read: tracks, trails, zones, new alerts."""
        self.store._require("track.read")
        now = now or _now()
        since = _iso(now - timedelta(seconds=window_s))
        rows = self.conn.execute(
            "SELECT * FROM tracks WHERE org_id = ? AND last_seen >= ? ORDER BY last_seen DESC LIMIT 500",
            (self.org, since)).fetchall()
        tracks = []
        for r in rows:
            age = (now - datetime.fromisoformat(r["last_seen"])).total_seconds()
            pts = self.conn.execute(
                "SELECT lon, lat FROM track_points WHERE track_id = ? ORDER BY t DESC LIMIT ?",
                (r["id"], trail)).fetchall()
            tracks.append({
                "id": r["id"], "uid": r["uid"], "sensor_id": r["sensor_id"], "kind": r["kind"],
                "lon": r["lon"], "lat": r["lat"], "speed_mps": r["speed_mps"], "heading_deg": r["heading_deg"],
                "first_seen": r["first_seen"], "last_seen": r["last_seen"], "points": r["points"],
                "age_s": round(age, 1), "state": "live" if age <= LOST_AFTER_S else "lost",
                "simulated": bool(r["simulated"]),
                "trail": [[p["lon"], p["lat"]] for p in pts][::-1]})
        new_alerts = [self._alert_dict(a) for a in self.conn.execute(
            "SELECT * FROM track_alerts WHERE org_id = ? AND seq > ? ORDER BY seq LIMIT 200",
            (self.org, since_alert)).fetchall()]
        cursor = self.conn.execute("SELECT COALESCE(MAX(seq), 0) FROM track_alerts WHERE org_id = ?",
                                   (self.org,)).fetchone()[0]
        pending = self.conn.execute("SELECT COUNT(*) FROM track_alerts WHERE org_id = ? AND status = 'pending'",
                                    (self.org,)).fetchone()[0]
        return {"as_of": _iso(now), "window_s": window_s, "tracks": tracks,
                "zones": [z.to_dict() for z in self.zones()], "sensors": self.sensors(),
                "alerts": new_alerts, "alert_cursor": cursor, "pending_alerts": pending,
                "lost_after_s": LOST_AFTER_S, "retention_hours": retention_hours()}

    def history(self, track_id: str) -> dict:
        self.store._require("track.read")
        r = self.conn.execute("SELECT * FROM tracks WHERE id = ? AND org_id = ?", (track_id, self.org)).fetchone()
        if r is None:
            raise TrackingError(f"no track {track_id} (it may have passed the retention window)")
        pts = self.conn.execute(
            "SELECT t, lon, lat, speed_mps, heading_deg FROM track_points WHERE track_id = ? ORDER BY t",
            (track_id,)).fetchall()
        self.store.audit("track.read", track_id, {"points": len(pts)})
        return {"id": r["id"], "uid": r["uid"], "sensor_id": r["sensor_id"], "kind": r["kind"],
                "first_seen": r["first_seen"], "last_seen": r["last_seen"], "simulated": bool(r["simulated"]),
                "points": [dict(p) for p in pts]}

    def replay(self, start: datetime, end: datetime) -> dict:
        self.store._require("track.read")
        if end <= start:
            raise TrackingError("'to' must be after 'from'")
        if (end - start) > timedelta(hours=6):
            raise TrackingError("replay at most 6 hours at a time")
        rows = self.conn.execute(
            "SELECT p.track_id, p.t, p.lon, p.lat, t.kind, t.simulated FROM track_points p"
            " JOIN tracks t ON t.id = p.track_id WHERE p.org_id = ? AND p.t >= ? AND p.t <= ?"
            " ORDER BY p.t LIMIT 50000", (self.org, _iso(start), _iso(end))).fetchall()
        self.store.audit("track.replay", f"{_iso(start)}/{_iso(end)}", {"points": len(rows)})
        return {"from": _iso(start), "to": _iso(end), "truncated": len(rows) >= 50000,
                "points": [{"track_id": r["track_id"], "t": r["t"], "lon": r["lon"], "lat": r["lat"],
                            "kind": r["kind"], "simulated": bool(r["simulated"])} for r in rows]}

    def _alert_dict(self, a) -> dict:
        return {"seq": a["seq"], "id": a["id"], "zone_id": a["zone_id"], "zone_name": a["zone_name"],
                "track_id": a["track_id"], "sensor_id": a["sensor_id"], "rule": a["rule"], "kind": a["kind"],
                "at": a["at"], "lon": a["lon"], "lat": a["lat"], "summary": a["summary"],
                "trail": json.loads(a["trail_json"]), "simulated": bool(a["simulated"]), "status": a["status"],
                "reviewed_by": a["reviewed_by"], "reviewed_at": a["reviewed_at"] or None, "note": a["note"]}

    def alerts(self, status: str = "", limit: int = 200) -> list[dict]:
        self.store._require("track.read")
        sql, args = "SELECT * FROM track_alerts WHERE org_id = ?", [self.org]
        if status:
            sql += " AND status = ?"
            args.append(status)
        rows = self.conn.execute(sql + " ORDER BY seq DESC LIMIT ?", (*args, limit)).fetchall()
        return [self._alert_dict(a) for a in rows]

    def review_alert(self, alert_id: str, status: str, note: str = "") -> dict:
        self.store._require("finding.escalate" if status == "escalated" else "finding.review")
        if status not in ("confirmed", "rejected", "escalated"):
            raise TrackingError("status must be confirmed, rejected or escalated")
        with self.conn:
            cur = self.conn.execute(
                "UPDATE track_alerts SET status = ?, reviewed_by = ?, reviewed_at = ?, note = ?"
                " WHERE id = ? AND org_id = ?", (status, self.store.actor, _iso(_now()), note[:2000], alert_id, self.org))
        if not cur.rowcount:
            raise TrackingError(f"no track alert {alert_id}")
        self.store.audit("track_alert.review", alert_id, {"status": status})
        row = self.conn.execute("SELECT * FROM track_alerts WHERE id = ?", (alert_id,)).fetchone()
        return self._alert_dict(row)
