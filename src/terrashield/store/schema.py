"""Database schema and migrations.

SQLite, deliberately. The PRD's target architecture is PostgreSQL with PostGIS
and that is the right production answer, but the deployment requirement that
actually constrains the design is section 39's: private cloud, on-premise, and
eventually air-gapped. A single file that an accreditor can copy to a stick,
open with the sqlite3 that ships on their laptop, and read without installing
anything is worth a great deal in that conversation. The repository layer keeps
SQL in one place so the move to PostGIS is a rewrite of one module.

Geometry is stored as GeoJSON text plus a precomputed bounding box. Without
PostGIS there is no spatial index, so the bbox columns are what make "which
AOIs intersect this area" a scan of four floats instead of a polygon test per
row.

Migrations are an ordered list. Each runs once, inside a transaction, and the
applied version is recorded. Never edit a migration that has shipped; add
another one.
"""

from __future__ import annotations

import sqlite3

MIGRATIONS: list[tuple[int, str]] = [
    (1, """
    CREATE TABLE organizations (
        id             TEXT PRIMARY KEY,
        name           TEXT NOT NULL,
        country        TEXT NOT NULL DEFAULT '',
        classification TEXT NOT NULL DEFAULT 'UNCLASSIFIED',
        created_at     TEXT NOT NULL
    );

    CREATE TABLE users (
        id         TEXT PRIMARY KEY,
        org_id     TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        name       TEXT NOT NULL,
        email      TEXT NOT NULL,
        role       TEXT NOT NULL,
        active     INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX idx_users_email ON users(org_id, email);

    CREATE TABLE aois (
        id            TEXT PRIMARY KEY,
        org_id        TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        name          TEXT NOT NULL,
        kind          TEXT NOT NULL,
        country       TEXT NOT NULL DEFAULT '',
        description   TEXT NOT NULL DEFAULT '',
        boundary_json TEXT NOT NULL,
        fingerprint   TEXT NOT NULL,
        area_km2      REAL NOT NULL,
        min_lon REAL NOT NULL, min_lat REAL NOT NULL,
        max_lon REAL NOT NULL, max_lat REAL NOT NULL,
        active        INTEGER NOT NULL DEFAULT 1,
        created_at    TEXT NOT NULL
    );
    CREATE INDEX idx_aois_org  ON aois(org_id);
    CREATE INDEX idx_aois_bbox ON aois(min_lon, min_lat, max_lon, max_lat);

    CREATE TABLE watchlists (
        id         TEXT PRIMARY KEY,
        org_id     TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        name       TEXT NOT NULL,
        priority   TEXT NOT NULL DEFAULT 'medium',
        created_at TEXT NOT NULL
    );
    CREATE TABLE watchlist_members (
        watchlist_id TEXT NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
        aoi_id       TEXT NOT NULL REFERENCES aois(id) ON DELETE CASCADE,
        PRIMARY KEY (watchlist_id, aoi_id)
    );

    CREATE TABLE scenes (
        id                TEXT PRIMARY KEY,
        aoi_id            TEXT NOT NULL REFERENCES aois(id) ON DELETE CASCADE,
        constellation     TEXT NOT NULL,
        sensor            TEXT NOT NULL,
        acquired_at       TEXT NOT NULL,
        acquired_on       TEXT NOT NULL,
        gsd_m             REAL NOT NULL,
        cloud_pct         REAL NOT NULL DEFAULT 0,
        off_nadir_deg     REAL NOT NULL DEFAULT 0,
        sun_elevation_deg REAL NOT NULL DEFAULT 0,
        orbit             TEXT NOT NULL DEFAULT '',
        usable            INTEGER NOT NULL DEFAULT 1,
        unusable_reason   TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX idx_scenes_aoi_date ON scenes(aoi_id, acquired_on);

    CREATE TABLE detections (
        id            TEXT PRIMARY KEY,
        aoi_id        TEXT NOT NULL REFERENCES aois(id) ON DELETE CASCADE,
        scene_id      TEXT NOT NULL,
        object_class  TEXT NOT NULL,
        confidence    REAL NOT NULL,
        lon REAL NOT NULL, lat REAL NOT NULL,
        geometry_json TEXT NOT NULL,
        extent_m      REAL NOT NULL DEFAULT 0,
        model_version TEXT NOT NULL DEFAULT '',
        observed_at   TEXT NOT NULL
    );
    CREATE INDEX idx_detections_aoi   ON detections(aoi_id, observed_at);
    CREATE INDEX idx_detections_scene ON detections(scene_id);

    CREATE TABLE change_events (
        id               TEXT PRIMARY KEY,
        aoi_id           TEXT NOT NULL REFERENCES aois(id) ON DELETE CASCADE,
        change_type      TEXT NOT NULL,
        detected_at      TEXT NOT NULL,
        before_scene_id  TEXT NOT NULL,
        after_scene_id   TEXT NOT NULL,
        geometry_json    TEXT NOT NULL,
        lon REAL NOT NULL, lat REAL NOT NULL,
        area_m2          REAL NOT NULL,
        confidence       REAL NOT NULL,
        magnitude        REAL NOT NULL,
        severity         TEXT NOT NULL,
        explanation      TEXT NOT NULL DEFAULT '',
        model_version    TEXT NOT NULL DEFAULT '',
        evidence_id      TEXT NOT NULL DEFAULT '',
        review_status    TEXT NOT NULL DEFAULT 'pending',
        reviewed_by      TEXT NOT NULL DEFAULT '',
        reviewed_at      TEXT,
        review_note      TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX idx_changes_aoi ON change_events(aoi_id, detected_at);

    CREATE TABLE observations (
        aoi_id   TEXT NOT NULL REFERENCES aois(id) ON DELETE CASCADE,
        metric   TEXT NOT NULL,
        when_on  TEXT NOT NULL,
        value    REAL NOT NULL,
        quality  REAL NOT NULL DEFAULT 1.0,
        scene_id TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (aoi_id, metric, when_on, scene_id)
    );
    CREATE INDEX idx_obs_aoi_metric ON observations(aoi_id, metric, when_on);

    CREATE TABLE anomalies (
        id            TEXT PRIMARY KEY,
        aoi_id        TEXT NOT NULL REFERENCES aois(id) ON DELETE CASCADE,
        observed_at   TEXT NOT NULL,
        score         REAL NOT NULL,
        confidence    REAL NOT NULL,
        severity      TEXT NOT NULL,
        baseline_n    INTEGER NOT NULL DEFAULT 0,
        reasons_json  TEXT NOT NULL DEFAULT '[]',
        contributions_json TEXT NOT NULL DEFAULT '{}',
        evidence_id   TEXT NOT NULL DEFAULT '',
        review_status TEXT NOT NULL DEFAULT 'pending'
    );
    CREATE INDEX idx_anomalies_aoi ON anomalies(aoi_id, observed_at);

    CREATE TABLE rules (
        id              TEXT PRIMARY KEY,
        org_id          TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        name            TEXT NOT NULL,
        definition_json TEXT NOT NULL,
        enabled         INTEGER NOT NULL DEFAULT 1,
        created_by      TEXT NOT NULL DEFAULT '',
        created_at      TEXT NOT NULL
    );

    CREATE TABLE alerts (
        id             TEXT PRIMARY KEY,
        org_id         TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        aoi_id         TEXT NOT NULL REFERENCES aois(id) ON DELETE CASCADE,
        rule_id        TEXT NOT NULL,
        title          TEXT NOT NULL,
        severity       TEXT NOT NULL,
        priority       INTEGER NOT NULL,
        created_at     TEXT NOT NULL,
        summary        TEXT NOT NULL DEFAULT '',
        payload_json   TEXT NOT NULL DEFAULT '{}',
        evidence_id    TEXT NOT NULL DEFAULT '',
        dedup_key      TEXT NOT NULL DEFAULT '',
        occurrences    INTEGER NOT NULL DEFAULT 1,
        review_status  TEXT NOT NULL DEFAULT 'pending',
        reviewed_by    TEXT NOT NULL DEFAULT '',
        reviewed_at    TEXT,
        review_note    TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX idx_alerts_org      ON alerts(org_id, created_at);
    CREATE INDEX idx_alerts_aoi      ON alerts(aoi_id, created_at);
    CREATE INDEX idx_alerts_dedup    ON alerts(dedup_key, created_at);
    CREATE INDEX idx_alerts_priority ON alerts(org_id, priority, created_at);

    CREATE TABLE evidence (
        id           TEXT PRIMARY KEY,
        aoi_id       TEXT NOT NULL REFERENCES aois(id) ON DELETE CASCADE,
        finding_id   TEXT NOT NULL,
        finding_kind TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        sha256       TEXT NOT NULL,
        body_json    TEXT NOT NULL
    );
    CREATE INDEX idx_evidence_finding ON evidence(finding_id);

    CREATE TABLE reports (
        id            TEXT PRIMARY KEY,
        org_id        TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        kind          TEXT NOT NULL,
        subject       TEXT NOT NULL,
        period_start  TEXT NOT NULL,
        period_end    TEXT NOT NULL,
        generated_at  TEXT NOT NULL,
        body_markdown TEXT NOT NULL DEFAULT '',
        alert_ids_json TEXT NOT NULL DEFAULT '[]'
    );

    CREATE TABLE audit_log (
        seq        INTEGER PRIMARY KEY,
        at         TEXT NOT NULL,
        actor      TEXT NOT NULL,
        org_id     TEXT NOT NULL,
        action     TEXT NOT NULL,
        subject    TEXT NOT NULL,
        detail_json TEXT NOT NULL DEFAULT '{}',
        prev_hash  TEXT NOT NULL DEFAULT '',
        hash       TEXT NOT NULL
    );
    CREATE INDEX idx_audit_actor ON audit_log(actor, at);
    CREATE INDEX idx_audit_org   ON audit_log(org_id, at);
    """),
    (2, """
    -- When the finding happened, as distinct from when its bundle was
    -- written. Exporting "the evidence for this site over the last six
    -- months" filtered on the row's creation time, which is the moment the
    -- pipeline ran -- so a pack built today for a period ending in March came
    -- back empty, and looked like a site with no findings rather than a query
    -- asking the wrong question.
    ALTER TABLE evidence ADD COLUMN finding_at TEXT NOT NULL DEFAULT '';
    CREATE INDEX idx_evidence_when ON evidence(aoi_id, finding_at);
    """),
]


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every migration that has not run. Returns the resulting version."""
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )""")
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_version")}
    current = max(applied) if applied else 0
    for version, sql in MIGRATIONS:
        if version in applied:
            continue
        with conn:
            conn.executescript(sql)
            conn.execute(
                "INSERT OR REPLACE INTO schema_version (version, applied_at) "
                "VALUES (?, datetime('now'))", (version,))
        current = max(current, version)
    return current
