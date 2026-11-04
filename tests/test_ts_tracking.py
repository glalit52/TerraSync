"""Live tracking, phase 1: intake, geofences, retention, isolation, the simulator."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from terrashield import simulate_tracks as sim
from terrashield.api import dispatch
from terrashield.domain import Organization, Role
from terrashield.rbac import AccessDenied
from terrashield.store import Store
from terrashield.tracking import (
    Report, Tracking, TrackingError, crosses_line, parse_cot, parse_json_report)

T0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
ORG, OTHER = "org-a", "org-b"
LINE = [[70.0, 24.0], [70.1, 24.0]]                       # east-west at 24.0
BOX = [[70.02, 24.01], [70.08, 24.01], [70.08, 24.03], [70.02, 24.03]]


def _store(db, org=ORG, role=Role.ADMIN, actor="op@example.com") -> Store:
    s = Store(db, org_id=org, actor=actor, role=role)
    s.put_org(Organization(org, org, "IN"))
    return s


@pytest.fixture()
def db(tmp_path):
    return str(tmp_path / "t.db")


@pytest.fixture()
def trk(db):
    s = _store(db)
    t = Tracking(s)
    t.register_sensor("Radar 1", "radar", 70.05, 23.99, 5000, sensor_id="r1")
    yield t
    s.close()


def rep(uid, lat, secs, kind="person", lon=70.05) -> Report:
    return Report(uid=uid, kind=kind, lon=lon, lat=lat, t=T0 + timedelta(seconds=secs))


def feed(trk, reports, now_secs=None):
    now = T0 + timedelta(seconds=now_secs if now_secs is not None else max(r.t for r in reports).timestamp() - T0.timestamp())
    return trk.ingest("r1", reports, now=now)


# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------

def test_json_reports_are_validated():
    r = parse_json_report({"uid": "a", "kind": "Vehicle", "lon": 70, "lat": 24, "time": "2026-11-04T10:00:00Z"})
    assert (r.kind, r.lon, r.lat) == ("vehicle", 70.0, 24.0)
    assert parse_json_report({"uid": "a", "kind": "tank", "lon": 70, "lat": 24}).kind == "unknown"
    for bad in ({"kind": "person", "lon": 70, "lat": 24}, {"uid": "a", "lon": 200, "lat": 24},
                {"uid": "a", "lon": "x", "lat": 24}, {"uid": "a", "lon": 70, "lat": 24, "time": "yesterday"}):
        with pytest.raises(TrackingError):
            parse_json_report(bad)


COT = ('<event version="2.0" uid="TRK-7" type="a-h-G-E-V" time="2026-11-04T10:00:00Z" '
       'start="2026-11-04T10:00:00Z" stale="2026-11-04T10:01:00Z" how="m-g">'
       '<point lat="24.0" lon="70.05" hae="0" ce="10" le="10"/>'
       '<detail><track course="45" speed="7.5"/><contact callsign="X"/></detail></event>')


def test_cot_gives_dimension_and_drops_affiliation():
    r = parse_cot(COT)
    assert (r.uid, r.kind, r.speed_mps, r.heading_deg) == ("TRK-7", "vehicle", 7.5, 45.0)
    # 'h' (hostile) is the affiliation: nothing on the report carries it.
    assert not any("h" == str(v) or "hostile" in str(v) for v in vars(r).values())
    assert parse_cot(COT.replace("a-h-G-E-V", "a-f-G-U-C-I")).kind == "person"
    assert parse_cot(COT.replace("a-h-G-E-V", "a-u-A-M-F-Q")).kind == "drone"


def test_cot_refuses_malformed_and_external_entities():
    for bad in ("<event", "<notevent/>", '<event uid="x" type="a-u-G"/>',
                '<!DOCTYPE e [<!ENTITY x SYSTEM "file:///etc/passwd">]><event uid="x">&x;</event>',
                "<event>" + "a" * 20_000 + "</event>"):
        with pytest.raises(TrackingError):
            parse_cot(bad)


def test_future_and_expired_reports_are_refused_with_reasons(trk):
    res = trk.ingest("r1", [rep("a", 23.99, 600), rep("b", 23.99, -400_000)], now=T0)
    assert res["accepted"] == 0
    assert any("future" in m for m in res["refused"])
    assert any("retention" in m for m in res["refused"])


def test_unknown_sensor_is_refused(trk):
    with pytest.raises(TrackingError):
        trk.ingest("nope", [rep("a", 23.99, 0)], now=T0)


def test_speed_and_heading_are_derived_when_not_sent(trk):
    feed(trk, [rep("a", 23.990, 0), rep("a", 23.991, 10)])
    t = trk.live(now=T0 + timedelta(seconds=10))["tracks"][0]
    assert t["speed_mps"] == pytest.approx(11.1, abs=0.2)      # 0.001 deg lat in 10 s
    assert t["heading_deg"] == pytest.approx(0, abs=0.5)       # due north


# ---------------------------------------------------------------------------
# Geofences
# ---------------------------------------------------------------------------

def test_segment_crossing():
    assert crosses_line((70.05, 23.99), (70.05, 24.01), [(70.0, 24.0), (70.1, 24.0)])
    assert not crosses_line((70.05, 23.99), (70.05, 23.995), [(70.0, 24.0), (70.1, 24.0)])
    assert not crosses_line((70.2, 23.99), (70.2, 24.01), [(70.0, 24.0), (70.1, 24.0)])


def test_line_crossing_alerts_once_and_debounces_jitter(trk):
    trk.put_zone("Fence", "line", LINE)
    out = [feed(trk, [rep("a", lat, s)]) for s, lat in
           [(0, 23.999), (2, 24.0005), (4, 23.9995), (6, 24.0006), (60, 23.999)]]
    alerts = [a for r in out for a in r["alerts"]]
    # Back-and-forth inside 30 s is one event; the return at 60 s is another.
    assert len(alerts) == 2
    assert "crossed Fence" in alerts[0]["summary"]


def test_no_entry_alerts_once_per_entry(trk):
    trk.put_zone("Strip", "no_entry", BOX)
    seq = [(0, 24.005), (2, 24.015), (4, 24.02), (6, 24.025), (8, 24.04), (60, 24.02)]
    alerts = [a for s, lat in seq for a in feed(trk, [rep("a", lat, s)])["alerts"]]
    assert [a["summary"] for a in alerts] == ["person track entered Strip"] * 2   # entry, exit, re-entry


def test_jitter_at_a_zone_edge_is_one_entry(trk):
    trk.put_zone("Strip", "no_entry", BOX)
    edge = [(0, 24.0099), (2, 24.0101), (4, 24.0099), (6, 24.0101), (8, 24.0099), (10, 24.0102)]
    alerts = [a for s, lat in edge for a in feed(trk, [rep("a", lat, s)])["alerts"]]
    assert len(alerts) == 1


def test_loiter_fires_at_the_limit_not_before(trk):
    trk.put_zone("Culvert", "loiter", BOX, loiter_s=60)
    early = [a for s in range(0, 60, 10) for a in feed(trk, [rep("a", 24.02, s)])["alerts"]]
    assert early == []
    late = feed(trk, [rep("a", 24.02, 61)])["alerts"] + feed(trk, [rep("a", 24.02, 120)])["alerts"]
    assert len(late) == 1 and "61 s" in late[0]["summary"]


def test_zone_kinds_filter(trk):
    trk.put_zone("Fence", "line", LINE, kinds=["vehicle"])
    alerts = [a for s, lat in [(0, 23.999), (2, 24.001)] for a in feed(trk, [rep("a", lat, s)])["alerts"]]
    assert alerts == []


def test_late_fixes_are_kept_but_raise_nothing(trk):
    trk.put_zone("Fence", "line", LINE)
    feed(trk, [rep("a", 24.001, 10)])
    res = feed(trk, [rep("a", 23.999, 5)], now_secs=11)       # older than the last fix
    assert res["accepted"] == 1 and res["alerts"] == []
    assert len(trk.history(trk.live(now=T0 + timedelta(seconds=11))["tracks"][0]["id"])["points"]) == 2


def test_alert_keeps_its_trail(trk):
    trk.put_zone("Fence", "line", LINE)
    for s, lat in [(0, 23.990), (2, 23.995), (4, 24.001)]:
        feed(trk, [rep("a", lat, s)])
    a = trk.alerts()[0]
    assert len(a["trail"]) == 3 and a["status"] == "pending"


def test_zone_validation(trk):
    with pytest.raises(TrackingError):
        trk.put_zone("x", "line", [[70, 24]])
    with pytest.raises(TrackingError):
        trk.put_zone("x", "no_entry", [[70, 24], [70.1, 24]])
    with pytest.raises(TrackingError):
        trk.put_zone("x", "loiter", BOX, loiter_s=3)
    with pytest.raises(TrackingError):
        trk.put_zone("x", "teleport", BOX)


# ---------------------------------------------------------------------------
# Retention, isolation, permissions, review, audit
# ---------------------------------------------------------------------------

def test_points_are_purged_after_retention_but_alert_trails_survive(trk, monkeypatch):
    monkeypatch.setenv("TERRASHIELD_TRACK_RETENTION_HOURS", "1")
    trk.put_zone("Fence", "line", LINE)
    feed(trk, [rep("a", 23.999, 0)])
    feed(trk, [rep("a", 24.001, 2)])
    trk.ingest("r1", [rep("b", 23.9, 7300)], now=T0 + timedelta(seconds=7300))
    live = trk.live(now=T0 + timedelta(seconds=7300), window_s=3600 * 3)
    assert [t["uid"] for t in live["tracks"]] == ["b"]
    assert len(trk.alerts()[0]["trail"]) == 2


def test_tenants_cannot_see_or_feed_each_other(db, trk):
    trk.put_zone("Fence", "line", LINE)
    feed(trk, [rep("a", 23.99, 0)])
    other = _store(db, org=OTHER)
    try:
        ot = Tracking(other)
        assert ot.live(now=T0)["tracks"] == [] and ot.zones() == [] and ot.sensors() == []
        with pytest.raises(TrackingError):
            ot.ingest("r1", [rep("x", 23.99, 0)], now=T0)
        with pytest.raises(TrackingError):
            ot.register_sensor("steal", "radar", sensor_id="r1")
    finally:
        other.close()


def test_viewers_read_but_cannot_feed_or_draw(db, trk):
    v = _store(db, role=Role.VIEWER, actor="viewer@example.com")
    try:
        vt = Tracking(v)
        vt.live(now=T0)
        with pytest.raises(AccessDenied):
            vt.ingest("r1", [rep("a", 23.99, 0)], now=T0)
        with pytest.raises(AccessDenied):
            vt.put_zone("z", "line", LINE)
    finally:
        v.close()


def test_review_and_escalation_permissions(db, trk):
    trk.put_zone("Strip", "no_entry", BOX)
    feed(trk, [rep("a", 24.02, 0)])
    aid = trk.alerts()[0]["id"]
    analyst = _store(db, role=Role.ANALYST, actor="analyst@example.com")
    try:
        at = Tracking(analyst)
        with pytest.raises(AccessDenied):
            at.review_alert(aid, "escalated")
        done = at.review_alert(aid, "confirmed", "seen on camera 3")
        assert (done["status"], done["reviewed_by"]) == ("confirmed", "analyst@example.com")
        with pytest.raises(TrackingError):
            at.review_alert(aid, "maybe")
    finally:
        analyst.close()


def test_history_reads_are_audited(trk):
    feed(trk, [rep("a", 23.99, 0)])
    tid = trk.live(now=T0)["tracks"][0]["id"]
    trk.history(tid)
    actions = [e["action"] for e in trk.store.audit_entries(50)]
    assert "track.read" in actions and "sensor.register" in actions


# ---------------------------------------------------------------------------
# The simulator, end to end
# ---------------------------------------------------------------------------

def test_simulated_cycle_raises_exactly_the_scripted_alerts(db):
    s = _store(db)
    try:
        t = Tracking(s)
        t.register_sensor("Sim", "simulator", *sim.ORIGIN, 6000, simulated=True, sensor_id=sim.SENSOR_ID)
        for z in sim.ZONES:
            t.put_zone(z["name"], z["rule"], z["geometry"], z["kinds"], z.get("loiter_s", 0), simulated=True)
        alerts = []
        for step in range(0, sim.CYCLE_S, 2):
            now = T0 + timedelta(seconds=step)
            reps = [parse_json_report({**r, "time": now.isoformat()}) for r in sim.reports_at(step)]
            alerts += t.ingest(sim.SENSOR_ID, reps, now=now)["alerts"]
        by = sorted(a["summary"] for a in alerts)
        assert by.count("person track crossed Demo fence line (simulated)") == 2
        assert by.count("person track entered Restricted strip (simulated)") == 2
        assert by.count("drone track crossed Demo fence line (simulated)") == 1
        # The drone is first seen already over the strip: that is an entry too.
        assert by.count("drone track entered Restricted strip (simulated)") == 1
        assert sum("Culvert" in a for a in by) == 1
        assert len(alerts) == 7                       # nothing from the patrol or the clutter
        live = t.live(now=T0 + timedelta(seconds=sim.CYCLE_S - 2))
        assert all(tr["simulated"] for tr in live["tracks"])
        assert all(a["simulated"] for a in t.alerts())
    finally:
        s.close()


def test_simulator_is_deterministic():
    assert sim.reports_at(95.0) == sim.reports_at(95.0)
    assert {r["uid"] for r in sim.reports_at(10)} >= {"patrol-1", "p0-1", "p0-2"}


# ---------------------------------------------------------------------------
# Over HTTP dispatch
# ---------------------------------------------------------------------------

def test_api_round_trip(db):
    s = _store(db)
    try:
        assert dispatch(s, "POST", "/api/sensors", {}, {"id": "gw", "name": "CoT gateway", "kind": "cot_gateway"})[0] == 200
        status, z = dispatch(s, "POST", "/api/zones", {}, {"name": "Fence", "rule": "line", "geometry": LINE})
        assert status == 200
        now = datetime.now(timezone.utc)
        body = {"sensor_id": "gw", "reports": [
            {"uid": "a", "kind": "person", "lon": 70.05, "lat": 23.999, "time": (now - timedelta(seconds=4)).isoformat()},
            {"uid": "a", "kind": "person", "lon": 70.05, "lat": 24.001, "time": now.isoformat()}],
            "cot": [COT.replace("2026-11-04T10:00:00Z", now.isoformat())]}
        status, res = dispatch(s, "POST", "/api/tracks/ingest", {}, body)
        assert status == 200 and res["accepted"] == 3 and len(res["alerts"]) == 1
        status, live = dispatch(s, "GET", "/api/tracks/live", {"since_alert": ["0"]})
        assert status == 200 and len(live["tracks"]) == 2 and live["pending_alerts"] == 1
        assert live["alert_cursor"] == live["alerts"][-1]["seq"]
        assert dispatch(s, "GET", "/api/tracks/live", {"since_alert": [str(live["alert_cursor"])]})[1]["alerts"] == []
        tid = live["tracks"][0]["id"]
        assert dispatch(s, "GET", f"/api/tracks/{tid}", {})[0] == 200
        frm, to = (now - timedelta(minutes=5)).isoformat(), (now + timedelta(minutes=1)).isoformat()
        assert dispatch(s, "GET", "/api/tracks/replay", {"from": [frm], "to": [to]})[1]["points"]
        assert dispatch(s, "GET", "/api/tracks/replay", {"from": [frm], "to": [(now + timedelta(hours=7)).isoformat()]})[0] == 400
        aid = live["alerts"][0]["id"]
        assert dispatch(s, "POST", f"/api/track-alerts/{aid}/review", {}, {"status": "confirmed"})[1]["alert"]["status"] == "confirmed"
        assert dispatch(s, "POST", f"/api/zones/{z['zone']['id']}/retire", {}, {})[1]["changed"] is True
        assert dispatch(s, "POST", "/api/tracks/ingest", {}, {"sensor_id": "gw", "reports": [{"uid": "x"}]})[0] == 400
        assert dispatch(s, "POST", "/api/tracks/ingest", {}, {"sensor_id": "zz", "reports": [
            {"uid": "x", "lon": 70, "lat": 24}]})[0] == 400
    finally:
        s.close()


def test_saving_an_organisation_again_does_not_cascade_away_its_data(db, trk):
    """Regression: put_org used INSERT OR REPLACE, whose delete cascaded."""
    trk.put_zone("Strip", "no_entry", BOX)
    feed(trk, [rep("a", 24.02, 0)])
    trk.store.put_org(Organization(ORG, "Renamed", "IN"))
    assert trk.store.get_org(ORG).name == "Renamed"
    assert len(trk.alerts()) == 1 and len(trk.zones()) == 1 and trk.sensors()
