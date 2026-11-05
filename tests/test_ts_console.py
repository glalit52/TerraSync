"""The analyst console's server side: static serving and the two routes it added."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from terrashield import sites
from terrashield.api import console_file, dispatch
from terrashield.baseline import Observation
from terrashield.domain import Organization, Role
from terrashield.store import Store


# ---------------------------------------------------------------------------
# Static serving
# ---------------------------------------------------------------------------

@pytest.fixture()
def build(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>console</title>")
    (root / "assets" / "index-abc.js").write_text("console.log(1)")
    (root / "assets" / "notes.unknown").write_text("x")
    (tmp_path / "secret.txt").write_text("outside the build")
    return root


def test_root_and_client_routes_serve_the_page(build):
    for path in ("/console", "/console/", "/console/alerts", "/console/sites/IN-MUN-PORT"):
        found = console_file(path, build)
        assert found is not None, path
        assert found[0].name == "index.html"
        assert found[1].startswith("text/html")


def test_assets_are_served_with_their_type(build):
    found = console_file("/console/assets/index-abc.js", build)
    assert found is not None
    assert found[1] == "text/javascript"


def test_a_missing_asset_is_not_answered_with_the_page(build):
    """A 404 for a missing script, not HTML the browser would try to run."""
    assert console_file("/console/assets/gone.js", build) is None


def test_traversal_out_of_the_build_is_refused(build):
    for path in ("/console/../secret.txt", "/console/assets/../../secret.txt",
                 "/console/..%2fsecret.txt"):
        assert console_file(path, build) is None, path


def test_a_doubled_slash_stays_inside_the_build(build):
    """`//etc/passwd` is read relative to the build: the page, never the host file."""
    found = console_file("/console//etc/passwd", build)
    assert found is not None
    assert found[0] == build.resolve() / "index.html"


def test_types_outside_the_allow_list_are_refused(build):
    assert console_file("/console/assets/notes.unknown", build) is None


def test_no_build_means_no_console(tmp_path):
    assert console_file("/console/", tmp_path / "missing") is None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path):
    db = str(tmp_path / "c.db")
    s = Store(db, org_id=sites.DEMO_ORG, actor="analyst@example.com", role=Role.ADMIN)
    s.put_org(Organization(sites.DEMO_ORG, "Demo", "IN"))
    demo = sites.load("mundra")
    s.put_aoi(demo.aoi)
    s.put_observations([
        Observation(aoi_id=demo.aoi.id, metric="vessel_count", when=date(2026, 5, d),
                    value=float(d), quality=1.0, scene_id=f"s{d}")
        for d in (1, 2, 3)
    ] + [Observation(aoi_id=demo.aoi.id, metric="truck_count", when=date(2026, 5, 2),
                     value=4.0, quality=0.8, scene_id="s2")])
    yield s, demo.aoi.id
    s.close()


def test_observations_are_grouped_by_metric(store):
    s, aoi = store
    status, body = dispatch(s, "GET", f"/api/sites/{aoi}/observations",
                            {"from": ["2026-04-01"], "to": ["2026-06-01"]})
    assert status == 200
    assert body["metrics"] == ["truck_count", "vessel_count"]
    assert [p["value"] for p in body["series"]["vessel_count"]] == [1.0, 2.0, 3.0]


def test_observations_respect_the_period_and_metric(store):
    s, aoi = store
    _, body = dispatch(s, "GET", f"/api/sites/{aoi}/observations",
                       {"from": ["2026-05-02"], "to": ["2026-05-02"],
                        "metric": ["vessel_count"]})
    assert body["metrics"] == ["vessel_count"]
    assert len(body["series"]["vessel_count"]) == 1


def test_observations_for_an_unknown_site_are_404(store):
    s, _ = store
    status, _ = dispatch(s, "GET", "/api/sites/nope/observations", {})
    assert status == 404


def test_providers_never_echo_a_credential(store, monkeypatch):
    s, _ = store
    monkeypatch.setenv("TERRASHIELD_PLANET_API_KEY", "sekrit-value-123")
    status, body = dispatch(s, "GET", "/api/providers", {})
    assert status == 200
    assert {"selected", "providers"} <= set(body)
    assert "sekrit-value-123" not in json.dumps(body)


def test_providers_are_readable_by_a_viewer(tmp_path):
    s = Store(str(tmp_path / "v.db"), org_id=sites.DEMO_ORG, actor="viewer@example.com",
              role=Role.VIEWER)
    try:
        status, _ = dispatch(s, "GET", "/api/providers", {})
        assert status == 200
    finally:
        s.close()


def test_changes_report_their_limit_and_truncation(store):
    s, _ = store
    status, body = dispatch(s, "GET", "/api/changes", {"limit": ["99999"]})
    assert status == 200
    assert body["limit"] == 5000              # clamped, never unbounded
    assert body["truncated"] is False
    assert body["changes"] == []


def test_a_non_numeric_limit_is_a_400(store):
    s, _ = store
    status, body = dispatch(s, "GET", "/api/changes", {"limit": ["lots"]})
    assert status == 400
    assert "limit" in body["error"]
