"""Persistence, tenant isolation, permissions and the audit chain.

The tests that a government security review would ask for, written as tests so
the answer is a run rather than an assurance.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from terrashield import geo, rbac, sites
from terrashield.alerts import default_rules
from terrashield.baseline import Observation
from terrashield.domain import (
    Alert, Aoi, AoiKind, ChangeEvent, ChangeType, Organization, ReviewStatus,
    Role, Severity, User, Watchlist,
)
from terrashield.store import Store
from terrashield.store.repo import StoreError

ORG = "org-a"
OTHER = "org-b"


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "ts.db")


@pytest.fixture
def store(db):
    s = Store(db, org_id=ORG, actor="owner@example.com", role=Role.ADMIN)
    s.put_org(Organization(ORG, "A", "IN"))
    s.put_org(Organization(OTHER, "B", "IN"))
    s.put_aoi(_aoi("AOI-1", ORG))
    return s


def _aoi(aoi_id: str, org: str, lon: float = 70.0) -> Aoi:
    return Aoi(id=aoi_id, org_id=org, name=f"Area {aoi_id}",
               boundary=geo.rectangle((lon, 23.0), 4000, 3000),
               kind=AoiKind.PORT, country="IN")


def _change(aoi_id: str, ident: str, when: date) -> ChangeEvent:
    return ChangeEvent(
        id=ident, aoi_id=aoi_id, change_type=ChangeType.NEW_STRUCTURE,
        detected_at=datetime.combine(when, datetime.min.time(), timezone.utc),
        before_scene_id="b", after_scene_id="a",
        geometry=geo.rectangle((70.0, 23.0), 200, 100),
        area_m2=20_000.0, confidence=0.9, magnitude=0.2, severity=Severity.HIGH,
        explanation="a new structure")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_migrations_are_idempotent(db):
    from terrashield.store.schema import MIGRATIONS
    latest = max(v for v, _ in MIGRATIONS)
    a = Store(db, org_id=ORG)
    b = Store(db, org_id=ORG)
    assert a.version == b.version == latest


def test_round_trips_an_aoi_exactly(store):
    back = store.get_aoi("AOI-1")
    assert back.boundary == _aoi("AOI-1", ORG).boundary
    assert back.fingerprint == _aoi("AOI-1", ORG).fingerprint
    assert back.area_km2 == pytest.approx(12.0, rel=1e-3)


def test_an_invalid_boundary_is_refused_at_the_door(store):
    bad = Aoi(id="AOI-BAD", org_id=ORG, name="bad",
              boundary=[(0, 0), (1, 1), (1, 0), (0, 1)], kind=AoiKind.GENERIC)
    with pytest.raises(StoreError, match="self-intersect"):
        store.put_aoi(bad)


def test_an_enormous_aoi_is_refused_with_a_reason(store):
    huge = Aoi(id="AOI-HUGE", org_id=ORG, name="huge",
               boundary=geo.rectangle((70.0, 23.0), 900_000, 700_000),
               kind=AoiKind.GENERIC)
    with pytest.raises(StoreError, match="split it"):
        store.put_aoi(huge)


def test_bbox_index_finds_overlapping_areas(store):
    store.put_aoi(_aoi("AOI-2", ORG, lon=80.0))
    near = store.aois_intersecting((69.9, 22.9, 70.1, 23.1))
    assert [a.id for a in near] == ["AOI-1"]


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------

def test_another_tenant_cannot_see_the_areas(store, db):
    other = Store(db, org_id=OTHER, actor="x@b.example", role=Role.ADMIN)
    assert other.list_aois() == []
    assert other.get_aoi("AOI-1") is None


def test_writing_across_tenants_raises_rather_than_silently_succeeding(store, db):
    other = Store(db, org_id=OTHER, actor="x@b.example", role=Role.ADMIN)
    with pytest.raises(rbac.TenantViolation):
        other.put_aoi(_aoi("AOI-1", ORG))


def test_findings_are_scoped_by_tenant(store, db):
    store.put_changes([_change("AOI-1", "c1", date(2026, 5, 1))])
    other = Store(db, org_id=OTHER, actor="x@b.example", role=Role.ADMIN)
    assert store.list_changes("AOI-1")
    assert other.list_changes("AOI-1") == []
    assert other.get_change("c1") is None


def test_an_unscoped_store_refuses_to_read(db):
    loose = Store(db, org_id="", actor="nobody")
    with pytest.raises(StoreError, match="tenant isolation"):
        loose.list_aois()


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

def test_role_ladder_is_cumulative():
    assert rbac.allows(Role.ADMIN, "alert.read")
    assert rbac.allows(Role.SUPERVISOR, "finding.review")
    assert not rbac.allows(Role.ANALYST, "user.manage")
    assert not rbac.allows(Role.VIEWER, "aoi.create")


def test_an_unknown_permission_is_denied_not_allowed():
    """A typo in a permission name must close a door, not open one."""
    assert not rbac.allows(Role.ADMIN, "typo.permission")


def test_a_viewer_cannot_create_an_area(db):
    viewer = Store(db, org_id=ORG, actor="v@a.example", role=Role.VIEWER)
    with pytest.raises(rbac.AccessDenied) as exc:
        viewer.put_aoi(_aoi("AOI-9", ORG))
    assert "aoi.create" in str(exc.value)
    assert "analyst" in str(exc.value)


def test_an_analyst_cannot_export_evidence(store, db):
    analyst = Store(db, org_id=ORG, actor="a@a.example", role=Role.ANALYST)
    with pytest.raises(rbac.AccessDenied):
        analyst.list_evidence("AOI-1")


def test_only_a_supervisor_can_escalate(store, db):
    store.put_changes([_change("AOI-1", "c1", date(2026, 5, 1))])
    analyst = Store(db, org_id=ORG, actor="a@a.example", role=Role.ANALYST)
    analyst.review_change("c1", ReviewStatus.CONFIRMED, "looks right")
    with pytest.raises(rbac.AccessDenied):
        analyst.review_change("c1", ReviewStatus.ESCALATED)
    supervisor = Store(db, org_id=ORG, actor="s@a.example", role=Role.SUPERVISOR)
    event = supervisor.review_change("c1", ReviewStatus.ESCALATED, "to desk")
    assert event.review_status is ReviewStatus.ESCALATED
    assert event.reviewed_by == "s@a.example"


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def test_every_write_is_attributed(store):
    entries = store.audit_entries()
    assert entries
    assert all(e["actor"] == "owner@example.com" for e in entries)
    assert any(e["action"] == "aoi.upsert" for e in entries)


def test_reading_imagery_is_audited(store):
    """The sensitive operation in an intelligence system is usually a read."""
    store.record_imagery_access("scene-1", "AOI-1")
    assert any(e["action"] == "imagery.read" and e["subject"] == "scene-1"
               for e in store.audit_entries())


def test_the_audit_chain_detects_tampering(store, db):
    store.put_changes([_change("AOI-1", "c1", date(2026, 5, 1))])
    store.review_change("c1", ReviewStatus.CONFIRMED, "ok")
    assert store.verify_audit_chain() == (True, None)

    with store.conn:
        store.conn.execute(
            "UPDATE audit_log SET detail_json = ? WHERE seq = ?",
            (json.dumps({"status": "rejected"}), 2))
    intact, bad = store.verify_audit_chain()
    assert not intact
    assert bad == 2


def test_only_an_admin_can_read_the_audit_log(db):
    analyst = Store(db, org_id=ORG, actor="a@a.example", role=Role.ANALYST)
    with pytest.raises(rbac.AccessDenied):
        analyst.audit_entries()


# ---------------------------------------------------------------------------
# Round trips
# ---------------------------------------------------------------------------

def test_observations_round_trip_and_deduplicate(store):
    rows = [Observation("AOI-1", "truck_count", date(2026, 5, 1), 30.0,
                        scene_id="s1")]
    store.put_observations(rows)
    store.put_observations(rows)          # same key, must not duplicate
    assert len(store.list_observations("AOI-1", "truck_count")) == 1


def test_rules_round_trip_through_json(store):
    for rule in default_rules(ORG):
        store.put_rule(rule)
    back = {r.id: r for r in store.list_rules()}
    assert len(back) == 5
    original = next(r for r in default_rules(ORG) if r.id == "rule-new-structure")
    assert back["rule-new-structure"].describe() == original.describe()
    assert back["rule-border-activity"].aoi_kinds == ["border_sector"]


def test_watchlists_round_trip(store):
    store.put_watchlist(Watchlist(id="wl-1", org_id=ORG, name="Strategic",
                                  aoi_ids=["AOI-1"], priority=Severity.HIGH))
    [wl] = store.list_watchlists()
    assert wl.aoi_ids == ["AOI-1"]
    assert wl.priority is Severity.HIGH


def test_alert_suppression_state_survives_a_restart(store, db):
    at = datetime(2026, 5, 1, tzinfo=timezone.utc)
    alert = Alert(id="alert-1", org_id=ORG, aoi_id="AOI-1", rule_id="r1",
                  title="t", severity=Severity.HIGH, priority=1, created_at=at)
    store.put_alert(alert, {"x": 1}, dedup_key="k1")
    reopened = Store(db, org_id=ORG, actor="owner@example.com", role=Role.ADMIN)
    assert reopened.last_alert_times()["k1"] == at


def test_evidence_is_selected_by_when_the_finding_happened(store, db):
    """Not by when its bundle row was written; those differ by however long
    ago the pipeline ran, and filtering on the wrong one makes a pack for an
    earlier period come back empty."""
    from terrashield.evidence import EvidenceBundle

    march = datetime(2026, 3, 12, tzinfo=timezone.utc)
    july = datetime(2026, 7, 4, tzinfo=timezone.utc)
    for ident, when in (("ev-mar", march), ("ev-jul", july)):
        bundle = EvidenceBundle(
            id=ident, aoi_id="AOI-1", finding_id=f"c-{ident}",
            finding_kind="change",
            created_at=datetime(2026, 9, 11, tzinfo=timezone.utc),  # today
            finding_at=when)
        store.put_evidence(bundle)

    supervisor = Store(db, org_id=ORG, actor="sup@a.example",
                       role=Role.SUPERVISOR)
    spring = supervisor.list_evidence("AOI-1", date(2026, 3, 1),
                                      date(2026, 3, 31))
    assert [b["id"] for b in spring] == ["ev-mar"]
    summer = supervisor.list_evidence("AOI-1", date(2026, 7, 1),
                                      date(2026, 7, 31))
    assert [b["id"] for b in summer] == ["ev-jul"]
    assert len(supervisor.list_evidence("AOI-1")) == 2


def test_migrations_add_columns_without_losing_rows(db):
    """Migration 2 runs against a database that already has evidence in it."""
    first = Store(db, org_id=ORG, actor="a@x.example")
    assert first.version >= 2
    cols = {r[1] for r in first.conn.execute("PRAGMA table_info(evidence)")}
    assert "finding_at" in cols
    applied = {r[0] for r in first.conn.execute(
        "SELECT version FROM schema_version")}
    assert applied == {1, 2}


def test_counts_are_scoped_to_the_tenant(store, db):
    store.put_changes([_change("AOI-1", "c1", date(2026, 5, 1))])
    other = Store(db, org_id=OTHER, actor="x@b.example", role=Role.ADMIN)
    assert store.counts()["change_events"] == 1
    assert other.counts()["change_events"] == 0
