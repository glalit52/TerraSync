"""The pipeline end to end, the copilot's refusals, the API and the CLI."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from terrashield import copilot, evidence, pipeline, reports, sites
from terrashield.alerts import default_rules
from terrashield.api import dispatch
from terrashield.catalog import SyntheticProvider
from terrashield.cli import main as cli_main
from terrashield.domain import Organization, ReviewStatus, Role, SiteStatus
from terrashield.store import Store

RUN = (date(2026, 3, 10), date(2026, 4, 5))


@pytest.fixture(scope="module")
def provider():
    p = SyntheticProvider()
    for demo in sites.load_all():
        p.register(demo.truth, demo.climate)
    return p


@pytest.fixture(scope="module")
def monitored(tmp_path_factory, provider):
    """One real pipeline run over one site. Shared: it is the slow fixture."""
    db = str(tmp_path_factory.mktemp("ts") / "run.db")
    store = Store(db, org_id=sites.DEMO_ORG, actor="analyst@sensegrass.com",
                  role=Role.ADMIN)
    store.put_org(Organization(sites.DEMO_ORG, "Sensegrass Demo", "IN"))
    for rule in default_rules(sites.DEMO_ORG):
        store.put_rule(rule)
    demo = sites.load("bhadla")
    store.put_aoi(demo.aoi)
    results = pipeline.run_range(store, provider, demo.aoi, *RUN)
    return db, store, demo, results


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def test_the_run_produces_findings(monitored):
    _, store, demo, results = monitored
    worked = [r for r in results if r.scene_used]
    assert len(worked) >= 5
    assert store.counts()["detections"] > 0
    assert store.counts()["change_events"] > 0
    assert store.counts()["anomalies"] > 0


def test_a_day_with_no_acquisition_is_recorded_not_skipped(monitored):
    """An empty timeline and an unobserved one are different statements."""
    _, _, _, results = monitored
    blind = [r for r in results if not r.scene_used]
    assert blind
    assert all(r.notes for r in blind)
    assert any("no acquisition" in n for r in blind for n in r.notes)


def test_every_stored_change_carries_an_evidence_bundle(monitored):
    _, store, demo, _ = monitored
    changes = store.list_changes(demo.id, limit=200)
    assert changes
    assert all(c.evidence_id for c in changes)
    doc = store.get_evidence(changes[0].evidence_id)
    assert doc is not None
    assert doc["finding_id"] == changes[0].id
    kinds = {a["kind"] for a in doc["artefacts"]}
    assert {"scene", "mask", "model", "parameter"} <= kinds


def test_evidence_records_what_could_not_be_established(monitored):
    _, store, demo, _ = monitored
    docs = [store.get_evidence(c.evidence_id)
            for c in store.list_changes(demo.id, limit=200)]
    assert any(d["gaps"] for d in docs), "no finding declared any gap"


def test_anomaly_evidence_always_carries_the_no_intent_caveat(monitored):
    _, store, demo, _ = monitored
    finding = store.list_anomalies(demo.id, limit=5)[0]
    doc = store.get_evidence(finding.evidence_id)
    assert any("no assessment of cause or intent" in g for g in doc["gaps"])


def test_the_run_is_idempotent(monitored, provider):
    """Re-running a date range must rewrite rows, never duplicate them."""
    _, store, demo, _ = monitored
    before = store.counts()
    pipeline.run_range(store, provider, demo.aoi, RUN[0], RUN[0] + timedelta(days=6))
    after = store.counts()
    assert after["change_events"] == before["change_events"]
    assert after["detections"] == before["detections"]


def test_arrivals_and_departures_are_metrics_not_change_events(monitored):
    _, store, demo, _ = monitored
    metrics = {o.metric for o in store.list_observations(demo.id)}
    assert "object_arrivals" in metrics
    assert all(c.change_type.value not in ("object_appeared", "object_departed")
               for c in store.list_changes(demo.id, limit=500))


def test_stored_alerts_carry_a_usable_deduplication_key(monitored):
    """Without this the suppression window never matches on the next run."""
    _, store, demo, _ = monitored
    rows = store.list_alerts(demo.id, limit=200)
    assert rows
    assert all(r["dedup_key"] for r in rows)
    for r in rows:
        # Keyed by place and kind, not by rule: one finding is one alert
        # however many rules noticed it.
        assert r["dedup_key"].startswith(f"{demo.id}|")
        assert r["rule_id"] not in r["dedup_key"]
    assert set(store.last_alert_times()) == {r["dedup_key"] for r in rows}


def test_audit_chain_survives_a_full_run(monitored):
    _, store, _, _ = monitored
    assert store.verify_audit_chain() == (True, None)


def test_health_reports_the_chain(monitored):
    _, store, _, _ = monitored
    h = pipeline.health(store)
    assert h["status"] == "ok"
    assert h["audit_chain_intact"]
    assert h["counts"]["aois"] == 1


def test_a_stale_site_is_reported_as_stale(provider, tmp_path):
    """No usable imagery for weeks is a fact worth surfacing, not an alert."""
    store = Store(str(tmp_path / "s.db"), org_id=sites.DEMO_ORG,
                  actor="a@x.example", role=Role.ADMIN)
    store.put_org(Organization(sites.DEMO_ORG, "D", "IN"))
    demo = sites.load("kutch")
    store.put_aoi(demo.aoi)
    #: Deep monsoon, and a day with no pass of its own.
    result = pipeline.run_day(store, provider, demo.aoi, date(2026, 8, 4),
                              rules=[], lookback_days=3)
    assert not result.scene_used
    assert result.status is SiteStatus.STALE
    assert any("no usable imagery" in n for n in result.notes)


# ---------------------------------------------------------------------------
# Evidence packs
# ---------------------------------------------------------------------------

def test_an_evidence_pack_verifies_and_detects_edits(monitored):
    _, store, demo, _ = monitored
    supervisor = Store(store.path, org_id=sites.DEMO_ORG,
                       actor="sup@sensegrass.com", role=Role.SUPERVISOR)
    docs = supervisor.list_evidence(demo.id)
    bundles = [evidence.EvidenceBundle(
        id=d["id"], aoi_id=d["aoi_id"], finding_id=d["finding_id"],
        finding_kind=d["finding_kind"],
        created_at=__import__("datetime").datetime.fromisoformat(d["created_at"]),
        artefacts=[evidence.Artefact(**a) for a in d["artefacts"]],
        measurements=d["measurements"], gaps=d["gaps"],
        narrative=d["narrative"]) for d in docs[:6]]
    pack = evidence.pack(bundles, demo.aoi, RUN)
    ok, problems = evidence.verify(pack)
    assert ok, problems

    pack["bundles"][0]["measurements"]["area_m2"] = 999_999
    ok, problems = evidence.verify(pack)
    assert not ok
    assert any("contents have changed" in p for p in problems)


def test_removing_a_bundle_breaks_the_manifest(monitored):
    _, store, demo, _ = monitored
    supervisor = Store(store.path, org_id=sites.DEMO_ORG,
                       actor="sup@sensegrass.com", role=Role.SUPERVISOR)
    docs = supervisor.list_evidence(demo.id)[:4]
    bundles = [evidence.EvidenceBundle(
        id=d["id"], aoi_id=d["aoi_id"], finding_id=d["finding_id"],
        finding_kind=d["finding_kind"],
        created_at=__import__("datetime").datetime.fromisoformat(d["created_at"]),
        artefacts=[evidence.Artefact(**a) for a in d["artefacts"]],
        measurements=d["measurements"], gaps=d["gaps"],
        narrative=d["narrative"]) for d in docs]
    pack = evidence.pack(bundles, demo.aoi, RUN)
    pack["bundles"].pop()
    ok, problems = evidence.verify(pack)
    assert not ok
    assert any("manifest" in p for p in problems)


# ---------------------------------------------------------------------------
# Copilot
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("question,fragment", [
    ("Is this a threat to the border?", "does not assess intent"),
    ("Whose facility is this?", "attribution of ownership"),
    ("Should we target the new structure?", "does not support targeting"),
    ("Predict what will happen next month", "no forecasting model"),
])
def test_the_copilot_refuses_questions_imagery_cannot_answer(
        monitored, question, fragment):
    _, store, _, _ = monitored
    answer = copilot.ask(store, question)
    assert answer.intent == "refused"
    assert answer.text == ""
    assert fragment in answer.unsupported


@pytest.mark.parametrize("phrasing", [
    "what changed at {name} in the last 60 days",
    "show me changes at {name}",
    "any new construction at {name}?",
])
def test_change_questions_route_correctly_whatever_the_word_form(
        monitored, phrasing):
    """`\\bchang\\b` matches nothing a person types. Prefix matching does."""
    _, store, demo, _ = monitored
    answer = copilot.ask(store, phrasing.format(name=demo.aoi.name),
                         today=RUN[1])
    assert answer.intent == "changes", answer.text


def test_the_copilot_answers_a_change_question_with_citations(monitored):
    _, store, demo, _ = monitored
    answer = copilot.ask(store, f"what changed at {demo.aoi.name} in the last "
                                "60 days", today=RUN[1])
    assert answer.intent == "changes"
    assert answer.rows
    assert answer.citations
    assert any(c.kind == "evidence" for c in answer.citations)


def test_every_number_in_an_answer_comes_from_a_row(monitored):
    """The grounding contract: no sentence the store cannot back."""
    _, store, demo, _ = monitored
    answer = copilot.ask(store, f"what changed at {demo.aoi.name}", today=RUN[1])
    assert str(len(answer.rows)) in answer.text


def test_the_copilot_says_when_it_cannot_resolve_the_area(monitored):
    _, store, _, _ = monitored
    answer = copilot.ask(store, "what changed at somewhere unmentioned")
    assert answer.intent == "unresolved"
    assert "does not name a monitored area" in answer.unsupported


def test_a_quiet_answer_points_at_coverage(monitored):
    """"No changes" must never be allowed to read as "nothing happened"."""
    _, store, demo, _ = monitored
    answer = copilot.ask(store, f"what changed at {demo.aoi.name} in the last "
                                "2 days", today=date(2026, 3, 11))
    if not answer.rows:
        assert "coverage" in answer.text


def test_coverage_questions_are_answered_from_the_scene_record(monitored):
    _, store, demo, _ = monitored
    answer = copilot.ask(store, f"what is the coverage at {demo.aoi.name}",
                         today=RUN[1])
    assert answer.intent == "coverage"
    assert answer.rows[0]["acquired"] > 0


def test_asking_is_audited(monitored):
    _, store, _, _ = monitored
    copilot.ask(store, "what needs attention today")
    assert any(e["action"] == "copilot.ask" for e in store.audit_entries(400))


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def test_a_weekly_report_leads_with_coverage(monitored):
    _, store, _, _ = monitored
    report = reports.weekly(store, sites.DEMO_ORG, RUN[1])
    body = report.body_markdown
    assert body.index("## Coverage") < body.index("## Significant changes")
    assert "no assessment of cause" in body


def test_a_site_report_names_its_evidence(monitored):
    _, store, demo, _ = monitored
    report = reports.site(store, demo.aoi, *RUN)
    assert demo.aoi.fingerprint in report.body_markdown
    assert "## Timeline" in report.body_markdown


def test_a_site_report_counts_only_alerts_from_its_own_period(monitored):
    """Otherwise the headline count disagrees with the timeline beneath it."""
    _, store, demo, _ = monitored
    inside = reports.site(store, demo.aoi, *RUN)
    before = reports.site(store, demo.aoi, date(2025, 1, 1), date(2025, 2, 1))
    assert "- Alerts: 0" in before.body_markdown
    assert "- Alerts: 0" not in inside.body_markdown or \
        not store.list_alerts(demo.id, limit=5)


def test_a_report_over_an_unobserved_period_says_so(monitored):
    _, store, demo, _ = monitored
    report = reports.site(store, demo.aoi, date(2025, 1, 1), date(2025, 2, 1))
    assert "No acquisitions at all in this period" in report.body_markdown


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def test_api_routes_return_what_the_prd_asks_for(monitored):
    _, store, demo, _ = monitored
    for path in ("/api/health", "/api/sites", "/api/alerts", "/api/rules"):
        status, payload = dispatch(store, "GET", path, {})
        assert status == 200, (path, payload)

    status, payload = dispatch(store, "GET", "/api/timeline",
                               {"site": [demo.id], "from": "2026-01-01",
                                "to": RUN[1].isoformat()})
    assert status == 200
    #: The timeline carries every acquisition, usable or not, so a cloudy
    #: month cannot be mistaken for a quiet one.
    stored = store.list_scenes(demo.id, date(2026, 1, 1), RUN[1])
    assert len(payload["acquisitions"]) == len(stored)
    assert len(payload["acquisitions"]) > len(
        [s for s in stored if s.usable]) - 1
    assert all("cloud_pct" in a and "usable" in a
               for a in payload["acquisitions"])


def test_api_reports_a_missing_site_as_404(monitored):
    _, store, _, _ = monitored
    status, payload = dispatch(store, "GET", "/api/sites/NOPE", {})
    assert status == 404
    assert "no monitored area" in payload["error"]


def test_api_requires_a_site_where_one_is_needed(monitored):
    _, store, _, _ = monitored
    status, payload = dispatch(store, "GET", "/api/objects", {})
    assert status == 400
    assert "site is required" in payload["error"]


def test_api_analysis_returns_findings_with_a_caveat(monitored):
    _, store, demo, _ = monitored
    status, payload = dispatch(store, "POST", "/api/analysis", {},
                               {"site": demo.id, "from": RUN[0].isoformat(),
                                "to": RUN[1].isoformat()})
    assert status == 200
    assert "no assessment of cause or intent" in payload["caveat"]
    assert payload["changes"]


@pytest.mark.parametrize("body,fragment", [
    ({"site": "X", "from": "not-a-date"}, "ISO date"),
    ({"site": "X", "to": "2026-13-45"}, "ISO date"),
    ({"site": "X", "from": "2026-05-01", "to": "2026-04-01"}, "is after"),
])
def test_api_analysis_rejects_bad_dates_as_400_not_500(monitored, body, fragment):
    """A typo in a caller's JSON is their error, and must not return a trace."""
    _, store, demo, _ = monitored
    status, payload = dispatch(store, "POST", "/api/analysis", {},
                               {**body, "site": demo.id})
    assert status == 400, payload
    assert fragment in payload["error"] or fragment in payload.get("hint", "")
    assert "trace" not in payload


def test_api_copilot_refusal_surfaces_as_a_200_with_a_reason(monitored):
    _, store, _, _ = monitored
    status, payload = dispatch(store, "POST", "/api/copilot", {},
                               {"question": "is this hostile activity"})
    assert status == 200
    assert payload["unsupported"]


def test_api_enforces_permissions(monitored):
    db, _, _, _ = monitored
    viewer = Store(db, org_id=sites.DEMO_ORG, actor="v@x.example",
                   role=Role.VIEWER)
    status, payload = dispatch(viewer, "POST", "/api/watchlists", {},
                               {"name": "x", "aoi_ids": []})
    assert status == 403
    assert "watchlist.manage" in payload["error"]


def test_api_unknown_route_lists_what_exists(monitored):
    _, store, _, _ = monitored
    status, payload = dispatch(store, "GET", "/api/nope", {})
    assert status == 404
    assert any("/api/sites" in r for r in payload["routes"])


def test_api_review_records_the_verdict(monitored):
    db, store, demo, _ = monitored
    change_id = store.list_changes(demo.id, limit=1)[0].id
    analyst = Store(db, org_id=sites.DEMO_ORG, actor="an@x.example",
                    role=Role.ANALYST)
    status, payload = dispatch(analyst, "POST", f"/api/reviews/{change_id}", {},
                               {"status": "confirmed", "note": "checked"})
    assert status == 200
    assert payload["change"]["review_status"] == "confirmed"
    assert payload["change"]["reviewed_by"] == "an@x.example"


def test_api_rejects_an_unknown_review_status(monitored):
    _, store, demo, _ = monitored
    change_id = store.list_changes(demo.id, limit=1)[0].id
    status, payload = dispatch(store, "POST", f"/api/reviews/{change_id}", {},
                               {"status": "vibes"})
    assert status == 400
    assert "must be one of" in payload["error"]


def test_serve_refuses_to_start_unauthenticated(tmp_path, monkeypatch):
    """A monitoring platform that defaults to open ships open."""
    from terrashield.api import serve
    monkeypatch.delenv("TERRASHIELD_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="refusing to serve"):
        serve(str(tmp_path / "x.db"))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_lifecycle(tmp_path, capsys):
    db = str(tmp_path / "cli.db")
    base = ["--db", db, "--org", sites.DEMO_ORG, "--actor", "cli@sensegrass.com"]
    assert cli_main([*base, "init"]) == 0
    assert cli_main([*base, "enroll", "bhadla"]) == 0
    assert cli_main([*base, "monitor", "IN-BHD-SOLAR",
                     "--from", "2026-03-10", "--to", "2026-03-26"]) == 0
    assert cli_main([*base, "queue"]) == 0
    assert cli_main([*base, "verify"]) == 0
    out = capsys.readouterr().out
    assert "default alert rules installed" in out
    assert "audit chain     intact" in out


def test_cli_ask_anchors_relative_periods_to_a_given_date(monitored, capsys):
    """"The last 30 days" from today finds nothing in a window that has closed."""
    db, store, demo, _ = monitored
    base = ["--db", db, "--org", sites.DEMO_ORG, "--actor", "cli@x.example"]
    question = f"what changed at {demo.aoi.name} in the last 30 days"

    assert cli_main([*base, "ask", question]) == 0
    stale = capsys.readouterr().out

    assert cli_main([*base, "ask", question, "--as-of", RUN[1].isoformat()]) == 0
    anchored = capsys.readouterr().out

    assert "No changes were recorded" in stale
    assert "change" in anchored and "No changes were recorded" not in anchored


def test_cli_refuses_to_act_without_an_actor(tmp_path):
    db = str(tmp_path / "cli2.db")
    with pytest.raises(SystemExit, match="--actor is required"):
        cli_main(["--db", db, "init"])


def test_cli_enrolls_from_geojson(tmp_path, capsys):
    from terrashield import geo
    db = str(tmp_path / "cli3.db")
    path = tmp_path / "sector.geojson"
    ring = geo.rectangle((71.0, 24.0), 6000, 4000)
    path.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [geo.ring_to_geojson(ring, {"name": "Sector 12"})]}))
    base = ["--db", db, "--org", sites.DEMO_ORG, "--actor", "cli@x.example"]
    assert cli_main([*base, "init"]) == 0
    assert cli_main([*base, "enroll", "--geojson", str(path),
                     "--name", "Sector 12", "--id", "AOI-S12",
                     "--kind", "border_sector"]) == 0
    assert "AOI-S12" in capsys.readouterr().out


def test_cli_rejects_a_bad_geojson_boundary(tmp_path):
    db = str(tmp_path / "cli4.db")
    path = tmp_path / "bad.geojson"
    path.write_text(json.dumps({
        "type": "Polygon",
        "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]}))
    base = ["--db", db, "--org", sites.DEMO_ORG, "--actor", "cli@x.example"]
    cli_main([*base, "init"])
    with pytest.raises(SystemExit, match="self-intersect"):
        cli_main([*base, "enroll", "--geojson", str(path)])


def test_cli_explain_renders_an_evidence_bundle(monitored, capsys):
    db, store, demo, _ = monitored
    change_id = store.list_changes(demo.id, limit=1)[0].id
    assert cli_main(["--db", db, "--org", sites.DEMO_ORG,
                     "--actor", "cli@x.example", "explain", change_id]) == 0
    out = capsys.readouterr().out
    assert "EVIDENCE" in out
    assert "based on" in out
    assert "bundle hash" in out


def test_cli_coverage_reports_gaps(capsys, tmp_path):
    db = str(tmp_path / "cov.db")
    base = ["--db", db, "--org", sites.DEMO_ORG, "--actor", "cli@x.example"]
    cli_main([*base, "init"])
    cli_main([*base, "enroll", "kutch"])
    assert cli_main([*base, "coverage", "--days", "120",
                     "--to", "2026-09-30"]) == 0
    out = capsys.readouterr().out
    assert "longest gap without a usable look" in out
    assert "rejected for cloud" in out


def test_movement_metrics_reach_the_scorer_not_only_the_baseline(monitored):
    """Appended after the assessment they were stored and never scored.

    `object_arrivals` and `object_departures` carry weights in anomaly.WEIGHTS.
    A weight on a metric the scorer never sees is a weight that cannot fire, so
    a port turning over three times its usual traffic would have scored on
    vessel count alone.
    """
    from terrashield.anomaly import WEIGHTS

    _, store, demo, results = monitored
    assert {"object_arrivals", "object_departures"} <= set(WEIGHTS)

    scored = [r for r in results
              if r.assessment and "object_arrivals" in r.assessment.readings]
    assert scored, "no run day carried the movement metrics into its readings"

    #: And they are recorded for future baselines, which is the other half.
    metrics = {o.metric for o in store.list_observations(demo.id)}
    assert {"object_arrivals", "object_departures"} <= metrics
