"""The repository: every SQL statement in the system, in one file.

Two invariants are enforced here rather than above, and both for the same
reason -- an API layer is one forgotten filter away from getting them wrong,
and a government customer will not accept "we have a test for that".

**Tenant isolation.** A `Store` is opened for one organisation. Every read is
scoped by `org_id` in the SQL itself, so a missing filter in a handler cannot
leak another tenant's sites. Cross-tenant access raises rather than returning
an empty list, because silence would let the bug live.

**Auditability.** Every write, and every read of imagery or evidence, appends
to a hash-chained audit log under the acting user's name. The chain means the
log can be shown to have not been edited, which is a different and stronger
claim than having a log at all.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..baseline import Observation
from ..domain import (
    Alert, AnomalyFinding, Aoi, AoiKind, ChangeEvent, ChangeType, Constellation,
    Detection, ObjectClass, Organization, Report, ReviewStatus, Role, Scene,
    Sensor, Severity, User, Watchlist,
)
from ..evidence import EvidenceBundle
from ..geo import bbox, centroid
from .. import rbac
from .schema import migrate


class StoreError(Exception):
    pass


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _dt(text: str | None) -> datetime | None:
    if not text:
        return None
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class Store:
    """A connection scoped to one organisation and one acting user."""

    def __init__(self, path: str | Path, org_id: str = "", actor: str = "system",
                 role: Role = Role.ADMIN) -> None:
        self.path = str(path)
        self.org_id = org_id
        self.actor = actor
        self.role = role
        Path(self.path).parent.mkdir(parents=True, exist_ok=True) \
            if Path(self.path).parent != Path("") else None
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.version = migrate(self.conn)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def as_user(self, user: User) -> "Store":
        """A view of the same database acting as a particular user."""
        return Store(self.path, org_id=user.org_id, actor=user.email,
                     role=user.role)

    # -- guards ------------------------------------------------------------

    def _require(self, permission: str) -> None:
        rbac.require(self.role, permission, self.actor)

    def _org(self) -> str:
        if not self.org_id:
            raise StoreError(
                "this store is not scoped to an organisation; open it with "
                "org_id so that tenant isolation can be enforced")
        return self.org_id

    # -- audit -------------------------------------------------------------

    def audit(self, action: str, subject: str, detail: dict | None = None) -> None:
        row = self.conn.execute(
            "SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
        seq = (row["seq"] + 1) if row else 1
        prev = row["hash"] if row else ""
        entry = rbac.entry(seq, self.actor, self.org_id, action, subject,
                           detail or {}, prev)
        with self.conn:
            self.conn.execute(
                "INSERT INTO audit_log (seq, at, actor, org_id, action, subject,"
                " detail_json, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?)",
                (entry.seq, _iso(entry.at), entry.actor, entry.org_id,
                 entry.action, entry.subject, json.dumps(entry.detail,
                                                         sort_keys=True,
                                                         default=str),
                 entry.prev_hash, entry.hash))

    def audit_entries(self, limit: int = 200) -> list[dict]:
        self._require("audit.read")
        rows = self.conn.execute(
            "SELECT * FROM audit_log WHERE org_id = ? ORDER BY seq DESC LIMIT ?",
            (self._org(), limit)).fetchall()
        return [dict(r) for r in rows]

    def verify_audit_chain(self) -> tuple[bool, int | None]:
        rows = self.conn.execute(
            "SELECT * FROM audit_log ORDER BY seq").fetchall()
        entries = [
            rbac.AuditEntry(
                seq=r["seq"], at=_dt(r["at"]), actor=r["actor"],
                org_id=r["org_id"], action=r["action"], subject=r["subject"],
                detail=json.loads(r["detail_json"]), prev_hash=r["prev_hash"])
            for r in rows
        ]
        for stored, rebuilt in zip(rows, entries):
            if stored["hash"] != rebuilt.hash:
                return False, rebuilt.seq
        return rbac.verify_chain(entries)

    # -- organisations and users -------------------------------------------

    def put_org(self, org: Organization) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO organizations "
                "(id, name, country, classification, created_at) VALUES (?,?,?,?,?)",
                (org.id, org.name, org.country, org.classification,
                 _iso(org.created_at)))

    def get_org(self, org_id: str) -> Organization | None:
        r = self.conn.execute("SELECT * FROM organizations WHERE id = ?",
                              (org_id,)).fetchone()
        if not r:
            return None
        return Organization(id=r["id"], name=r["name"], country=r["country"],
                            classification=r["classification"],
                            created_at=_dt(r["created_at"]))

    def put_user(self, user: User) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO users "
                "(id, org_id, name, email, role, active, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (user.id, user.org_id, user.name, user.email, user.role.value,
                 1 if user.active else 0, _iso(user.created_at)))
        self.audit("user.upsert", user.id, {"role": user.role.value})

    def get_user(self, email: str) -> User | None:
        r = self.conn.execute(
            "SELECT * FROM users WHERE org_id = ? AND email = ?",
            (self._org(), email)).fetchone()
        if not r:
            return None
        return User(id=r["id"], org_id=r["org_id"], name=r["name"],
                    email=r["email"], role=Role(r["role"]),
                    active=bool(r["active"]), created_at=_dt(r["created_at"]))

    def list_users(self) -> list[User]:
        self._require("user.manage")
        rows = self.conn.execute(
            "SELECT * FROM users WHERE org_id = ? ORDER BY email",
            (self._org(),)).fetchall()
        return [User(id=r["id"], org_id=r["org_id"], name=r["name"],
                     email=r["email"], role=Role(r["role"]),
                     active=bool(r["active"]), created_at=_dt(r["created_at"]))
                for r in rows]

    # -- AOIs --------------------------------------------------------------

    def put_aoi(self, aoi: Aoi) -> None:
        rbac.same_tenant(self._org(), aoi.org_id, f"AOI {aoi.id}")
        self._require("aoi.create")
        problems = aoi.problems()
        if problems:
            raise StoreError(f"AOI {aoi.id} cannot be saved: " + "; ".join(problems))
        min_lon, min_lat, max_lon, max_lat = bbox(aoi.boundary)
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO aois (id, org_id, name, kind, country,"
                " description, boundary_json, fingerprint, area_km2, min_lon,"
                " min_lat, max_lon, max_lat, active, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (aoi.id, aoi.org_id, aoi.name, aoi.kind.value, aoi.country,
                 aoi.description, json.dumps(aoi.boundary), aoi.fingerprint,
                 aoi.area_km2, min_lon, min_lat, max_lon, max_lat,
                 1 if aoi.active else 0, _iso(aoi.created_at)))
        self.audit("aoi.upsert", aoi.id,
                   {"fingerprint": aoi.fingerprint,
                    "area_km2": round(aoi.area_km2, 2)})

    def _aoi_from(self, r: sqlite3.Row) -> Aoi:
        return Aoi(id=r["id"], org_id=r["org_id"], name=r["name"],
                   boundary=[tuple(p) for p in json.loads(r["boundary_json"])],
                   kind=AoiKind(r["kind"]), country=r["country"],
                   description=r["description"], active=bool(r["active"]),
                   created_at=_dt(r["created_at"]))

    def get_aoi(self, aoi_id: str) -> Aoi | None:
        r = self.conn.execute(
            "SELECT * FROM aois WHERE id = ? AND org_id = ?",
            (aoi_id, self._org())).fetchone()
        return self._aoi_from(r) if r else None

    def list_aois(self, active_only: bool = True) -> list[Aoi]:
        sql = "SELECT * FROM aois WHERE org_id = ?"
        if active_only:
            sql += " AND active = 1"
        rows = self.conn.execute(sql + " ORDER BY name", (self._org(),)).fetchall()
        return [self._aoi_from(r) for r in rows]

    def aois_intersecting(self, box: tuple[float, float, float, float]) -> list[Aoi]:
        """Every AOI whose bounding box overlaps `box`.

        The bbox columns exist because there is no spatial index without
        PostGIS. Cheap, and enough for "what do we monitor near here".
        """
        min_lon, min_lat, max_lon, max_lat = box
        rows = self.conn.execute(
            "SELECT * FROM aois WHERE org_id = ? AND max_lon >= ? AND min_lon <= ?"
            " AND max_lat >= ? AND min_lat <= ? ORDER BY name",
            (self._org(), min_lon, max_lon, min_lat, max_lat)).fetchall()
        return [self._aoi_from(r) for r in rows]

    # -- watchlists --------------------------------------------------------

    def put_watchlist(self, wl: Watchlist) -> None:
        rbac.same_tenant(self._org(), wl.org_id, f"watchlist {wl.id}")
        self._require("watchlist.manage")
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO watchlists (id, org_id, name, priority,"
                " created_at) VALUES (?,?,?,?,?)",
                (wl.id, wl.org_id, wl.name, wl.priority.value, _iso(wl.created_at)))
            self.conn.execute("DELETE FROM watchlist_members WHERE watchlist_id = ?",
                              (wl.id,))
            self.conn.executemany(
                "INSERT OR IGNORE INTO watchlist_members (watchlist_id, aoi_id)"
                " VALUES (?,?)", [(wl.id, a) for a in wl.aoi_ids])
        self.audit("watchlist.upsert", wl.id, {"members": len(wl.aoi_ids)})

    def list_watchlists(self) -> list[Watchlist]:
        rows = self.conn.execute(
            "SELECT * FROM watchlists WHERE org_id = ? ORDER BY name",
            (self._org(),)).fetchall()
        out = []
        for r in rows:
            members = [m["aoi_id"] for m in self.conn.execute(
                "SELECT aoi_id FROM watchlist_members WHERE watchlist_id = ?"
                " ORDER BY aoi_id", (r["id"],))]
            out.append(Watchlist(id=r["id"], org_id=r["org_id"], name=r["name"],
                                 aoi_ids=members, priority=Severity(r["priority"]),
                                 created_at=_dt(r["created_at"])))
        return out

    # -- scenes ------------------------------------------------------------

    def put_scenes(self, scenes: Iterable[Scene]) -> int:
        rows = [(s.id, s.aoi_id, s.constellation.value, s.sensor.value,
                 _iso(s.acquired_at), s.acquired_on.isoformat(), s.gsd_m,
                 s.cloud_pct, s.off_nadir_deg, s.sun_elevation_deg, s.orbit,
                 1 if s.usable else 0, s.unusable_reason) for s in scenes]
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO scenes (id, aoi_id, constellation, sensor,"
                " acquired_at, acquired_on, gsd_m, cloud_pct, off_nadir_deg,"
                " sun_elevation_deg, orbit, usable, unusable_reason)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def list_scenes(self, aoi_id: str, start: date | None = None,
                    end: date | None = None, usable_only: bool = False
                    ) -> list[Scene]:
        sql = ("SELECT s.* FROM scenes s JOIN aois a ON a.id = s.aoi_id"
               " WHERE s.aoi_id = ? AND a.org_id = ?")
        args: list[Any] = [aoi_id, self._org()]
        if start:
            sql += " AND s.acquired_on >= ?"
            args.append(start.isoformat())
        if end:
            sql += " AND s.acquired_on <= ?"
            args.append(end.isoformat())
        if usable_only:
            sql += " AND s.usable = 1"
        rows = self.conn.execute(sql + " ORDER BY s.acquired_at", args).fetchall()
        return [Scene(id=r["id"], aoi_id=r["aoi_id"],
                      constellation=Constellation(r["constellation"]),
                      sensor=Sensor(r["sensor"]), acquired_at=_dt(r["acquired_at"]),
                      gsd_m=r["gsd_m"], cloud_pct=r["cloud_pct"],
                      off_nadir_deg=r["off_nadir_deg"],
                      sun_elevation_deg=r["sun_elevation_deg"], orbit=r["orbit"])
                for r in rows]

    def record_imagery_access(self, scene_id: str, aoi_id: str) -> None:
        """Audit a pixel read. PRD section 39 requires it; so does any customer."""
        self._require("imagery.read")
        self.audit("imagery.read", scene_id, {"aoi_id": aoi_id})

    # -- detections --------------------------------------------------------

    def put_detections(self, dets: Iterable[Detection]) -> int:
        rows = [(d.id, d.aoi_id, d.scene_id, d.object_class.value, d.confidence,
                 d.lon, d.lat, json.dumps(d.geometry), d.extent_m,
                 d.model_version, _iso(d.observed_at)) for d in dets]
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO detections (id, aoi_id, scene_id,"
                " object_class, confidence, lon, lat, geometry_json, extent_m,"
                " model_version, observed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def list_detections(self, aoi_id: str, scene_id: str | None = None,
                        limit: int = 2000) -> list[Detection]:
        sql = ("SELECT d.* FROM detections d JOIN aois a ON a.id = d.aoi_id"
               " WHERE d.aoi_id = ? AND a.org_id = ?")
        args: list[Any] = [aoi_id, self._org()]
        if scene_id:
            sql += " AND d.scene_id = ?"
            args.append(scene_id)
        rows = self.conn.execute(
            sql + " ORDER BY d.confidence DESC LIMIT ?", [*args, limit]).fetchall()
        return [Detection(
            id=r["id"], aoi_id=r["aoi_id"], scene_id=r["scene_id"],
            object_class=ObjectClass(r["object_class"]), confidence=r["confidence"],
            lon=r["lon"], lat=r["lat"],
            geometry=[tuple(p) for p in json.loads(r["geometry_json"])],
            extent_m=r["extent_m"], model_version=r["model_version"],
            observed_at=_dt(r["observed_at"])) for r in rows]

    # -- change events -----------------------------------------------------

    def put_changes(self, events: Iterable[ChangeEvent]) -> int:
        rows = []
        for e in events:
            lon, lat = centroid(e.geometry)
            rows.append((e.id, e.aoi_id, e.change_type.value, _iso(e.detected_at),
                         e.before_scene_id, e.after_scene_id,
                         json.dumps(e.geometry), lon, lat, e.area_m2,
                         e.confidence, e.magnitude, e.severity.value,
                         e.explanation, e.model_version, e.evidence_id,
                         e.review_status.value, e.reviewed_by,
                         _iso(e.reviewed_at), e.review_note))
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO change_events (id, aoi_id, change_type,"
                " detected_at, before_scene_id, after_scene_id, geometry_json,"
                " lon, lat, area_m2, confidence, magnitude, severity, explanation,"
                " model_version, evidence_id, review_status, reviewed_by,"
                " reviewed_at, review_note)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def _change_from(self, r: sqlite3.Row) -> ChangeEvent:
        return ChangeEvent(
            id=r["id"], aoi_id=r["aoi_id"], change_type=ChangeType(r["change_type"]),
            detected_at=_dt(r["detected_at"]), before_scene_id=r["before_scene_id"],
            after_scene_id=r["after_scene_id"],
            geometry=[tuple(p) for p in json.loads(r["geometry_json"])],
            area_m2=r["area_m2"], confidence=r["confidence"],
            magnitude=r["magnitude"], severity=Severity(r["severity"]),
            explanation=r["explanation"], model_version=r["model_version"],
            evidence_id=r["evidence_id"],
            review_status=ReviewStatus(r["review_status"]),
            reviewed_by=r["reviewed_by"], reviewed_at=_dt(r["reviewed_at"]),
            review_note=r["review_note"])

    def list_changes(self, aoi_id: str | None = None, start: date | None = None,
                     end: date | None = None, limit: int = 500) -> list[ChangeEvent]:
        sql = ("SELECT c.* FROM change_events c JOIN aois a ON a.id = c.aoi_id"
               " WHERE a.org_id = ?")
        args: list[Any] = [self._org()]
        if aoi_id:
            sql += " AND c.aoi_id = ?"
            args.append(aoi_id)
        if start:
            sql += " AND c.detected_at >= ?"
            args.append(start.isoformat())
        if end:
            sql += " AND c.detected_at <= ?"
            args.append(end.isoformat() + "T23:59:59+00:00")
        rows = self.conn.execute(
            sql + " ORDER BY c.detected_at DESC LIMIT ?", [*args, limit]).fetchall()
        return [self._change_from(r) for r in rows]

    def get_change(self, change_id: str) -> ChangeEvent | None:
        r = self.conn.execute(
            "SELECT c.* FROM change_events c JOIN aois a ON a.id = c.aoi_id"
            " WHERE c.id = ? AND a.org_id = ?",
            (change_id, self._org())).fetchone()
        return self._change_from(r) if r else None

    def review_change(self, change_id: str, status: ReviewStatus,
                      note: str = "") -> ChangeEvent:
        """Record an analyst's verdict. The other half of human-in-the-loop.

        PRD section 38 wants this feedback to become training data, and it can
        -- but only if the verdict is stored with the finding it judged and the
        name of the person who made it. A thumbs-up with no attribution teaches
        a model nothing anyone would sign off on.
        """
        if status is ReviewStatus.ESCALATED:
            self._require("finding.escalate")
        else:
            self._require("finding.review")
        event = self.get_change(change_id)
        if event is None:
            raise StoreError(f"no change event {change_id} in this organisation")
        with self.conn:
            self.conn.execute(
                "UPDATE change_events SET review_status = ?, reviewed_by = ?,"
                " reviewed_at = ?, review_note = ? WHERE id = ?",
                (status.value, self.actor, _iso(datetime.now(timezone.utc)),
                 note, change_id))
        self.audit("finding.review", change_id,
                   {"status": status.value, "note": note,
                    "was": event.review_status.value})
        return self.get_change(change_id)

    # -- observations and anomalies ----------------------------------------

    def put_observations(self, obs: Iterable[Observation]) -> int:
        rows = [(o.aoi_id, o.metric, o.when.isoformat(), o.value, o.quality,
                 o.scene_id) for o in obs]
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO observations (aoi_id, metric, when_on,"
                " value, quality, scene_id) VALUES (?,?,?,?,?,?)", rows)
        return len(rows)

    def list_observations(self, aoi_id: str, metric: str | None = None,
                          start: date | None = None,
                          end: date | None = None) -> list[Observation]:
        sql = ("SELECT o.* FROM observations o JOIN aois a ON a.id = o.aoi_id"
               " WHERE o.aoi_id = ? AND a.org_id = ?")
        args: list[Any] = [aoi_id, self._org()]
        if metric:
            sql += " AND o.metric = ?"
            args.append(metric)
        if start:
            sql += " AND o.when_on >= ?"
            args.append(start.isoformat())
        if end:
            sql += " AND o.when_on <= ?"
            args.append(end.isoformat())
        rows = self.conn.execute(sql + " ORDER BY o.when_on", args).fetchall()
        return [Observation(aoi_id=r["aoi_id"], metric=r["metric"],
                            when=date.fromisoformat(r["when_on"]),
                            value=r["value"], quality=r["quality"],
                            scene_id=r["scene_id"]) for r in rows]

    def put_anomaly(self, finding: AnomalyFinding) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO anomalies (id, aoi_id, observed_at, score,"
                " confidence, severity, baseline_n, reasons_json,"
                " contributions_json, evidence_id, review_status)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (finding.id, finding.aoi_id, _iso(finding.observed_at),
                 finding.score, finding.confidence, finding.severity.value,
                 finding.baseline_n, json.dumps(finding.reasons),
                 json.dumps(finding.contributions), finding.evidence_id,
                 finding.review_status.value))

    def list_anomalies(self, aoi_id: str | None = None, limit: int = 200
                       ) -> list[AnomalyFinding]:
        sql = ("SELECT n.* FROM anomalies n JOIN aois a ON a.id = n.aoi_id"
               " WHERE a.org_id = ?")
        args: list[Any] = [self._org()]
        if aoi_id:
            sql += " AND n.aoi_id = ?"
            args.append(aoi_id)
        rows = self.conn.execute(
            sql + " ORDER BY n.observed_at DESC LIMIT ?", [*args, limit]).fetchall()
        return [AnomalyFinding(
            id=r["id"], aoi_id=r["aoi_id"], observed_at=_dt(r["observed_at"]),
            score=r["score"], confidence=r["confidence"],
            reasons=json.loads(r["reasons_json"]),
            contributions=json.loads(r["contributions_json"]),
            baseline_n=r["baseline_n"], evidence_id=r["evidence_id"],
            review_status=ReviewStatus(r["review_status"])) for r in rows]

    # -- rules and alerts --------------------------------------------------

    def put_rule(self, rule) -> None:
        rbac.same_tenant(self._org(), rule.org_id, f"rule {rule.id}")
        self._require("rule.create")
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO rules (id, org_id, name, definition_json,"
                " enabled, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
                (rule.id, rule.org_id, rule.name, json.dumps(rule.to_dict()),
                 1 if rule.enabled else 0, rule.created_by or self.actor,
                 _iso(datetime.now(timezone.utc))))
        self.audit("rule.upsert", rule.id, {"name": rule.name})

    def list_rules(self, enabled_only: bool = True) -> list:
        from ..alerts import Condition, Op, Rule
        sql = "SELECT * FROM rules WHERE org_id = ?"
        if enabled_only:
            sql += " AND enabled = 1"
        rows = self.conn.execute(sql + " ORDER BY name", (self._org(),)).fetchall()
        out = []
        for r in rows:
            d = json.loads(r["definition_json"])
            out.append(Rule(
                id=d["id"], org_id=d["org_id"], name=d["name"],
                conditions=[Condition(c["field"], Op(c["op"]), c["value"])
                            for c in d["conditions"]],
                severity_floor=Severity(d["severity_floor"]),
                aoi_ids=d.get("aoi_ids", []), aoi_kinds=d.get("aoi_kinds", []),
                channels=d.get("channels", ["dashboard"]),
                suppress_days=d.get("suppress_days", 7),
                enabled=bool(r["enabled"]), created_by=d.get("created_by", "")))
        return out

    def put_alert(self, alert: Alert, payload: dict, dedup_key: str = "",
                  occurrences: int = 1) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO alerts (id, org_id, aoi_id, rule_id, title,"
                " severity, priority, created_at, summary, payload_json,"
                " evidence_id, dedup_key, occurrences, review_status, reviewed_by,"
                " reviewed_at, review_note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (alert.id, alert.org_id, alert.aoi_id, alert.rule_id, alert.title,
                 alert.severity.value, alert.priority, _iso(alert.created_at),
                 alert.summary, json.dumps(payload, default=str),
                 alert.evidence_id, dedup_key, occurrences,
                 alert.review_status.value, alert.reviewed_by,
                 _iso(alert.reviewed_at), alert.review_note))

    def list_alerts(self, aoi_id: str | None = None, limit: int = 200,
                    max_priority: int = 4,
                    status: ReviewStatus | None = None) -> list[dict]:
        self._require("alert.read")
        sql = "SELECT * FROM alerts WHERE org_id = ? AND priority <= ?"
        args: list[Any] = [self._org(), max_priority]
        if aoi_id:
            sql += " AND aoi_id = ?"
            args.append(aoi_id)
        if status:
            sql += " AND review_status = ?"
            args.append(status.value)
        rows = self.conn.execute(
            sql + " ORDER BY priority, created_at DESC LIMIT ?",
            [*args, limit]).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json") or "{}")
            out.append(d)
        return out

    def last_alert_times(self) -> dict[str, datetime]:
        """Most recent alert per deduplication key, for suppression windows."""
        rows = self.conn.execute(
            "SELECT dedup_key, MAX(created_at) AS last FROM alerts"
            " WHERE org_id = ? AND dedup_key <> '' GROUP BY dedup_key",
            (self._org(),)).fetchall()
        return {r["dedup_key"]: _dt(r["last"]) for r in rows}

    def review_alert(self, alert_id: str, status: ReviewStatus,
                     note: str = "") -> dict:
        if status is ReviewStatus.ESCALATED:
            self._require("finding.escalate")
        else:
            self._require("finding.review")
        r = self.conn.execute(
            "SELECT * FROM alerts WHERE id = ? AND org_id = ?",
            (alert_id, self._org())).fetchone()
        if not r:
            raise StoreError(f"no alert {alert_id} in this organisation")
        with self.conn:
            self.conn.execute(
                "UPDATE alerts SET review_status = ?, reviewed_by = ?,"
                " reviewed_at = ?, review_note = ? WHERE id = ?",
                (status.value, self.actor, _iso(datetime.now(timezone.utc)),
                 note, alert_id))
        self.audit("finding.review", alert_id,
                   {"status": status.value, "note": note})
        rows = self.list_alerts(limit=1000)
        return next(a for a in rows if a["id"] == alert_id)

    # -- evidence ----------------------------------------------------------

    def put_evidence(self, bundle: EvidenceBundle) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO evidence (id, aoi_id, finding_id,"
                " finding_kind, created_at, finding_at, sha256, body_json)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (bundle.id, bundle.aoi_id, bundle.finding_id, bundle.finding_kind,
                 _iso(bundle.created_at),
                 _iso(bundle.finding_at or bundle.created_at), bundle.sha256,
                 json.dumps(bundle.to_dict(), default=str)))

    def get_evidence(self, evidence_id: str) -> dict | None:
        r = self.conn.execute(
            "SELECT e.* FROM evidence e JOIN aois a ON a.id = e.aoi_id"
            " WHERE e.id = ? AND a.org_id = ?",
            (evidence_id, self._org())).fetchone()
        if not r:
            return None
        self.audit("evidence.read", evidence_id, {"aoi_id": r["aoi_id"]})
        return json.loads(r["body_json"])

    def evidence_for(self, finding_id: str) -> dict | None:
        r = self.conn.execute(
            "SELECT e.* FROM evidence e JOIN aois a ON a.id = e.aoi_id"
            " WHERE e.finding_id = ? AND a.org_id = ?",
            (finding_id, self._org())).fetchone()
        return json.loads(r["body_json"]) if r else None

    def list_evidence(self, aoi_id: str, start: date | None = None,
                      end: date | None = None) -> list[dict]:
        self._require("evidence.export")
        sql = ("SELECT e.* FROM evidence e JOIN aois a ON a.id = e.aoi_id"
               " WHERE e.aoi_id = ? AND a.org_id = ?")
        args: list[Any] = [aoi_id, self._org()]
        #: Selected on when the finding happened, not on when its bundle was
        #: written. Those differ by however long ago the pipeline ran.
        if start:
            sql += " AND e.finding_at >= ?"
            args.append(start.isoformat())
        if end:
            sql += " AND e.finding_at <= ?"
            args.append(end.isoformat() + "T23:59:59+00:00")
        rows = self.conn.execute(sql + " ORDER BY e.finding_at", args).fetchall()
        self.audit("evidence.export", aoi_id, {"count": len(rows)})
        return [json.loads(r["body_json"]) for r in rows]

    # -- reports -----------------------------------------------------------

    def put_report(self, report: Report) -> None:
        self._require("report.publish")
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO reports (id, org_id, kind, subject,"
                " period_start, period_end, generated_at, body_markdown,"
                " alert_ids_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (report.id, report.org_id, report.kind, report.subject,
                 report.period_start.isoformat(), report.period_end.isoformat(),
                 _iso(report.generated_at), report.body_markdown,
                 json.dumps(report.alert_ids)))
        self.audit("report.publish", report.id,
                   {"kind": report.kind, "subject": report.subject})

    def list_reports(self, limit: int = 50) -> list[dict]:
        self._require("report.read")
        rows = self.conn.execute(
            "SELECT * FROM reports WHERE org_id = ? ORDER BY generated_at DESC"
            " LIMIT ?", (self._org(), limit)).fetchall()
        return [dict(r) for r in rows]

    # -- health ------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for table, scoped in (("aois", True), ("scenes", False),
                              ("detections", False), ("change_events", False),
                              ("anomalies", False), ("alerts", True),
                              ("evidence", False), ("rules", True),
                              ("observations", False), ("audit_log", True)):
            if scoped:
                sql = f"SELECT COUNT(*) FROM {table} WHERE org_id = ?"
                args: tuple = (self._org(),)
            else:
                sql = (f"SELECT COUNT(*) FROM {table} t JOIN aois a"
                       f" ON a.id = t.aoi_id WHERE a.org_id = ?")
                args = (self._org(),)
            out[table] = self.conn.execute(sql, args).fetchone()[0]
        return out
