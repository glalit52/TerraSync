"""AIS correlation and the evidence graph.

The maritime half is mostly about restraint. A detection with no matching AIS
report is the single easiest place in this product to say something defamatory
about a named ship, so the tests hold the vocabulary as tightly as they hold
the arithmetic.

The graph half is about one query: walking back from a conclusion to the pixels
it rests on. An evidence chain that ends in a node nobody can open is a chain
that fails its own audit.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from terrashield.ais import (
    ASSUMED_SPEED_KTS, BASE_TOLERANCE_M, AisReport, Correlated,
    VesselDetection, correlate, correlate_all, summarise,
)
from terrashield.domain import (
    Aoi, AoiKind, ChangeEvent, ChangeType, Constellation, Scene, Sensor,
    Severity,
)
from terrashield.events import fuse
from terrashield.geo import rectangle
from terrashield.graph import Graph, NodeKind, Relation, build

T = datetime(2026, 7, 14, 6, 30, tzinfo=timezone.utc)
PORT = "IN-MUN-PORT"


def det(ident, lon=69.700, lat=22.840, conf=0.9, length=180.0):
    return VesselDetection(f"d{ident}", PORT, T, lon, lat, length, conf)


def rep(mmsi, lon, lat, mins=0.0, speed=12.0, name="", source="terrestrial"):
    return AisReport(mmsi, T + timedelta(minutes=mins), lon, lat, name,
                     speed, None, source)


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------

def test_a_nearby_contemporaneous_report_matches():
    c = correlate(det(1), [rep("419000001", 69.7005, 22.8403, 2, 10, "MV Konkan")])
    assert c.status is Correlated.MATCHED
    assert c.matched
    assert c.distance_m < 200
    assert "MV Konkan" in c.headline()


def test_a_stale_report_on_a_fast_vessel_still_matches():
    """A fixed radius is wrong in both directions. A ship at 20 knots covers
    600 m a minute, and a 20-minute-old report is still the best evidence
    available about where it was."""
    c = correlate(det(2), [rep("419000002", 69.760, 22.840, -20, 20)])
    assert c.matched
    assert c.distance_m > 5000, "far away, but reachable in the gap"


def test_a_stale_report_on_a_slow_vessel_does_not_match_the_same_distance():
    c = correlate(det(3), [rep("419000003", 69.760, 22.840, -20, 0.5)])
    assert not c.matched, "a near-stationary vessel cannot have moved that far"


def test_a_report_outside_the_time_window_is_not_considered():
    c = correlate(det(4), [rep("419000004", 69.7001, 22.8400, 90)],
                  window=timedelta(minutes=30))
    assert c.status is Correlated.UNCORRELATED
    assert c.reports_in_window == 0


def test_the_closest_plausible_report_wins():
    c = correlate(det(5), [
        rep("far", 69.7030, 22.8400, 1, 10),
        rep("near", 69.7002, 22.8400, 1, 10),
        rep("mid", 69.7015, 22.8400, 1, 10)])
    assert c.report.mmsi == "near"


# ---------------------------------------------------------------------------
# What it refuses to say
# ---------------------------------------------------------------------------

def test_an_unmatched_detection_is_uncorrelated_and_never_accused():
    c = correlate(det(6), [rep("419000006", 69.900, 22.940, 1, 5)])
    assert c.status is Correlated.UNCORRELATED
    text = c.headline().lower()
    assert "did not correlate" in text
    assert "not evidence about the vessel" in text
    for word in ("dark", "evading", "illicit", "suspicious", "smuggl",
                 "hostile", "spoofing", "going dark", "non-compliant"):
        assert word not in text, f"{word!r} accuses rather than reports"


def test_every_uncorrelated_finding_carries_the_innocent_explanations():
    """They reach the analyst with the finding rather than being something
    they have to remember from a manual."""
    c = correlate(det(7), [])
    assert len(c.caveats) >= 4
    blob = " ".join(c.caveats).lower()
    for reason in ("class b", "satellite ais", "fishing", "may not be a vessel"):
        assert reason in blob, reason


def test_no_ais_coverage_is_distinguished_from_no_match():
    """'We could not look' and 'we looked and found nothing' are different
    facts, and collapsing them overstates the second."""
    c = correlate(det(8), [], has_coverage=False)
    assert c.status is Correlated.NO_COVERAGE
    assert "could not be attempted" in c.headline()
    assert c.status is not Correlated.UNCORRELATED


def test_the_summary_explains_what_uncorrelated_means():
    results = [correlate(det(9), []),
               correlate(det(10), [rep("m", 69.7001, 22.8400, 1)])]
    s = summarise(results)
    assert s["matched"] == 1 and s["uncorrelated"] == 1
    assert "not a finding about any vessel's conduct" in s["note"]


# ---------------------------------------------------------------------------
# Many at once
# ---------------------------------------------------------------------------

def test_two_detections_cannot_share_one_transponder():
    """Two ships in an anchorage must not both match the same report."""
    results = correlate_all(
        [det(11, 69.7000, 22.8400, conf=0.95),
         det(12, 69.7010, 22.8402, conf=0.80)],
        [rep("419000011", 69.7001, 22.8400, 1, 8, "MV A")])
    by_id = {r.detection.id: r for r in results}
    assert by_id["d11"].status is Correlated.MATCHED
    assert by_id["d12"].status is Correlated.UNCORRELATED


def test_the_more_confident_detection_gets_first_claim():
    results = correlate_all(
        [det(13, 69.7010, 22.8402, conf=0.60),
         det(14, 69.7000, 22.8400, conf=0.99)],
        [rep("419000013", 69.7001, 22.8400, 1, 8)])
    matched = [r for r in results if r.matched]
    assert len(matched) == 1
    assert matched[0].detection.id == "d14"


def test_every_detection_appears_in_the_results():
    detections = [det(i, 69.70 + i * 0.01) for i in range(5)]
    results = correlate_all(detections, [])
    assert len(results) == 5
    assert {r.detection.id for r in results} == {d.id for d in detections}


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------

@pytest.fixture
def estate():
    aoi = Aoi("IN-KCH-SECTOR", "org", "Rann of Kutch",
              rectangle((70.1, 23.9), 4000, 3000), AoiKind.BORDER_SECTOR)
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    scenes = [Scene(f"s2:{i}", aoi.id, Constellation.SENTINEL_2, Sensor.OPTICAL,
                    start + timedelta(days=i * 4), 10.0, 5.0, 2.0, 60.0,
                    "133-des", "ck") for i in range(4)]
    geom = {"type": "Polygon", "coordinates": [[
        [70.1, 23.9], [70.101, 23.9], [70.101, 23.901], [70.1, 23.901],
        [70.1, 23.9]]]}
    changes = [ChangeEvent(
        f"chg-{i}", aoi.id, ChangeType.NEW_STRUCTURE,
        start + timedelta(days=i * 4), scenes[max(0, i - 1)].id, scenes[i].id,
        geom, 900.0 + i * 600, 0.9, 0.5, Severity.HIGH, "measured", "v1",
        f"ev-{i}") for i in range(4)]
    events = fuse(changes)
    alerts = [{"id": "alert-1", "title": "New structure at a monitored site",
               "finding_id": events[0].id, "evidence_id": "ev-3"}]
    return aoi, scenes, changes, events, alerts


def test_the_evidence_path_reaches_the_source(estate):
    """The question an analyst asks is 'why am I being shown this?', and the
    answer has to end at the pixels."""
    aoi, scenes, changes, events, alerts = estate
    g = build(aoi, changes=changes, events=events, alerts=alerts, scenes=scenes)
    path = g.evidence_path("alert-1")
    kinds = [n.kind for n in path]

    assert kinds[0] is NodeKind.ALERT
    for required in (NodeKind.EVENT, NodeKind.CHANGE, NodeKind.SCENE,
                     NodeKind.SOURCE, NodeKind.SENSOR, NodeKind.EVIDENCE,
                     NodeKind.MODEL):
        assert required in kinds, f"{required} missing from the chain"


def test_every_node_on_the_path_resolves(estate):
    """A chain ending in a node nobody can open fails its own audit."""
    aoi, scenes, changes, events, alerts = estate
    g = build(aoi, changes=changes, events=events, alerts=alerts, scenes=scenes)
    for node in g.evidence_path("alert-1"):
        assert node.id in g.nodes


def test_a_dangling_edge_is_refused_rather_than_recorded(estate):
    g = Graph()
    g.add("a", NodeKind.ALERT, "a")
    assert g.link("a", Relation.RAISED_BY, "does-not-exist") is None
    assert g.link("missing", Relation.RAISED_BY, "a") is None
    assert g.stats()["edges"] == 0


def test_adding_the_same_node_twice_is_idempotent():
    g = Graph()
    g.add("x", NodeKind.AREA, "First")
    g.add("x", NodeKind.AREA, "Second")
    assert len(g.nodes) == 1
    assert g.nodes["x"].label == "Second"


def test_an_attribute_named_like_a_parameter_does_not_collide():
    """`kind`, `label` and `id` are exactly the attribute names a geospatial
    record has, which is why attrs is a dict and not **kwargs."""
    g = Graph()
    node = g.add("n1", NodeKind.AREA, "Area",
                 {"kind": "port", "label": "other", "id": "something"})
    assert node.kind is NodeKind.AREA
    assert node.label == "Area"
    assert node.attrs["kind"] == "port"


def test_the_subgraph_is_bounded_by_depth(estate):
    aoi, scenes, changes, events, alerts = estate
    g = build(aoi, changes=changes, events=events, alerts=alerts, scenes=scenes)
    near = g.subgraph(events[0].id, depth=1)
    far = g.subgraph(events[0].id, depth=3)
    assert len(near["nodes"]) < len(far["nodes"])
    assert near["root"] == events[0].id


def test_an_unknown_node_yields_nothing_rather_than_raising():
    g = Graph()
    assert g.evidence_path("nope") == []
    assert g.subgraph("nope") == {"nodes": [], "edges": []}


def test_the_ontology_has_nowhere_to_record_who_did_something():
    """The same boundary domain.py holds. Adding an actor type would be a
    reviewable act, not an oversight."""
    names = {k.value for k in NodeKind}
    for forbidden in ("person", "actor", "operator", "owner", "suspect",
                      "unit", "adversary"):
        assert forbidden not in names


def test_no_relation_expresses_cause_or_intent():
    names = {r.value for r in Relation}
    for forbidden in ("causes", "caused_by", "intends", "targets",
                      "responsible_for", "attributed_to"):
        assert forbidden not in names


def test_a_correlated_vessel_appears_in_the_graph_but_an_uncorrelated_one_has_no_edge(estate):
    """An absence of evidence is not a relationship."""
    aoi, scenes, changes, events, alerts = estate
    matched = correlate(det(20, 70.1, 23.9),
                        [rep("419000020", 70.1001, 23.9, 1, 8, "MV B")])
    unmatched = correlate(det(21, 70.1, 23.9), [])
    g = build(aoi, scenes=scenes, correlations=[matched, unmatched])

    assert "d20" in g.nodes and "d21" in g.nodes
    matches = [e for edges in g.out.values() for e in edges
               if e.rel is Relation.MATCHES]
    assert len(matches) == 1
    assert matches[0].src == "d20"


def test_graph_stats_describe_what_was_built(estate):
    aoi, scenes, changes, events, alerts = estate
    g = build(aoi, changes=changes, events=events, alerts=alerts, scenes=scenes)
    stats = g.stats()
    assert stats["nodes"] > 10
    assert stats["by_kind"]["scene"] == len(scenes)
    assert stats["by_kind"]["change"] == len(changes)
