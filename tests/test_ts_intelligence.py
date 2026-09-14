"""Baselines, anomaly scoring, risk, rules and the queue.

The recurring theme: these components are judged on what they *refuse* to say
as much as on what they compute. A baseline that drifts toward an anomaly, a
score that rises for a quiet Sunday, an alert queue that fires forty times for
one construction site -- each is a way of being technically correct and
commercially useless.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from terrashield import alerts as alert_engine
from terrashield import geo, risk
from terrashield.world import Rng, seed_of
from terrashield.anomaly import MetricReading, assess, headline
from terrashield.baseline import (
    MIN_SAMPLES, Observation, build, build_all, trend,
)
from terrashield.domain import (
    AnomalyFinding, Aoi, AoiKind, ChangeEvent, ChangeType, Severity,
)

AOI = Aoi(id="AOI-T", org_id="org", name="Test area",
          boundary=geo.rectangle((70.0, 23.0), 4000, 3000), kind=AoiKind.PORT)
BORDER = Aoi(id="AOI-B", org_id="org", name="Sector",
             boundary=geo.rectangle((70.5, 23.9), 5000, 3600),
             kind=AoiKind.BORDER_SECTOR)


def weekly_history(metric="truck_count", weekday=34.0, weekend=11.0,
                   days=120, start=date(2026, 1, 1)):
    return [Observation("AOI-T", metric, start + timedelta(days=i),
                        weekday if (start + timedelta(days=i)).weekday() < 5
                        else weekend)
            for i in range(days)]


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------

def test_weekday_term_stops_every_sunday_being_an_anomaly():
    """Without it, a port alerts once a week forever and gets muted."""
    b = build(weekly_history(), "truck_count", date(2026, 5, 1), "fp")
    assert b.has_weekday_term
    saturday = date(2026, 5, 2)
    assert abs(b.deviation(saturday, 12.0)) < 3.0
    # Against a weekday median of 34 the counting-noise floor is sqrt(34), so a
    # jump to 60 is a little over four deviations rather than the eight a flat
    # floor would have reported.
    assert b.deviation(date(2026, 5, 1), 60.0) > 4.0


def test_baseline_uses_only_history_strictly_before_the_day_assessed():
    rows = weekly_history(days=40)
    rows.append(Observation("AOI-T", "truck_count", date(2026, 2, 10), 900.0))
    b = build(rows, "truck_count", date(2026, 2, 10), "fp")
    assert b.overall.median < 40
    assert b.overall.window_end < date(2026, 2, 10)


def test_median_resists_a_run_of_extreme_days():
    """A slow build-up must not be absorbed as the new normal."""
    rows = weekly_history(days=100)
    for i in range(100, 112):
        rows.append(Observation("AOI-T", "truck_count",
                                date(2026, 1, 1) + timedelta(days=i), 400.0))
    b = build(rows, "truck_count", date(2026, 4, 30), "fp")
    assert b.overall.median < 60
    assert b.deviation(date(2026, 4, 30), 400.0) > 5


def test_low_quality_observations_are_excluded():
    rows = [Observation("AOI-T", "m", date(2026, 1, 1) + timedelta(days=i),
                        10.0, quality=0.2) for i in range(30)]
    assert build(rows, "m", date(2026, 3, 1), "fp") is None


def test_zero_variance_baseline_does_not_produce_infinite_deviation():
    rows = [Observation("AOI-T", "m", date(2026, 1, 1) + timedelta(days=i), 0.0)
            for i in range(30)]
    b = build(rows, "m", date(2026, 3, 1), "fp")
    z = b.deviation(date(2026, 3, 1), 20.0)
    assert 0 < z < 1e6
    assert b.deviation(date(2026, 3, 1), 1.0) < 2.0


def test_a_collapsed_mad_falls_back_to_counting_noise():
    """A small count must not make every ordinary day a three-sigma event."""
    rows = [Observation("AOI-T", "m", date(2026, 1, 1) + timedelta(days=i), 4.0)
            for i in range(30)]
    b = build(rows, "m", date(2026, 3, 1), "fp")
    assert b.deviation(date(2026, 3, 1), 6.0) < 1.5      # ordinary wobble
    assert b.deviation(date(2026, 3, 1), 30.0) > 10.0    # a real excursion


def test_infrastructure_counts_do_not_drive_the_activity_score():
    """Buildings do not come and go between passes; the variation is the detector."""
    from terrashield.anomaly import STRUCTURAL_METRICS
    rows = [Observation("AOI-T", "building_count",
                        date(2026, 1, 1) + timedelta(days=i), 4.0)
            for i in range(60)]
    b = build_all(rows, date(2026, 3, 10), AOI.fingerprint)
    a = assess(AOI, date(2026, 3, 10),
               {"building_count": MetricReading("building_count", 40.0)}, b)
    assert "building_count" in STRUCTURAL_METRICS
    assert a.finding.score == 0.0
    assert "not an activity signal" in a.skipped["building_count"]


def test_trend_detects_a_slow_build_up():
    rows = [Observation("AOI-T", "m", date(2026, 1, 1) + timedelta(days=i),
                        10.0 + 0.5 * i) for i in range(60)]
    assert trend(rows, "m", date(2026, 3, 1)) == pytest.approx(0.5, rel=0.1)


def test_build_all_covers_every_metric():
    rows = weekly_history() + weekly_history(metric="vessel_count",
                                             weekday=8.0, weekend=3.0)
    out = build_all(rows, date(2026, 5, 1), "fp")
    assert set(out) == {"truck_count", "vessel_count"}


# ---------------------------------------------------------------------------
# Anomaly
# ---------------------------------------------------------------------------

def _baselines():
    return build_all(weekly_history() + weekly_history(
        metric="vessel_count", weekday=8.0, weekend=3.0),
        date(2026, 5, 1), AOI.fingerprint)


def test_quiet_days_do_not_score():
    """Only excursions above normal count; otherwise the queue fills with Sundays."""
    a = assess(AOI, date(2026, 5, 1),
               {"truck_count": MetricReading("truck_count", 6.0)},
               _baselines())
    assert a.finding.score == 0.0


def test_a_large_excursion_scores_high_with_named_drivers():
    a = assess(AOI, date(2026, 5, 1),
               {"truck_count": MetricReading("truck_count", 95.0),
                "vessel_count": MetricReading("vessel_count", 24.0)},
               _baselines())
    assert a.finding.score >= 65
    assert set(a.finding.contributions) == {"truck_count", "vessel_count"}
    assert any("truck count is 95" in r for r in a.finding.reasons)


def test_score_combines_as_a_soft_maximum_not_a_sum():
    """Six mildly odd metrics must not outrank one metric six deviations out."""
    b = _baselines()
    many = assess(AOI, date(2026, 5, 1),
                  {"truck_count": MetricReading("truck_count", 44.0),
                   "vessel_count": MetricReading("vessel_count", 10.0)}, b)
    one = assess(AOI, date(2026, 5, 1),
                 {"truck_count": MetricReading("truck_count", 140.0)}, b)
    assert one.finding.score > many.finding.score


def test_confidence_falls_with_a_short_history_not_with_the_deviation():
    short = build_all(weekly_history(days=8), date(2026, 1, 9), AOI.fingerprint)
    a = assess(AOI, date(2026, 1, 9),
               {"truck_count": MetricReading("truck_count", 500.0)}, short)
    assert a.finding.score > 0
    assert a.finding.confidence < 0.6
    assert any("provisional" in r for r in a.finding.reasons)


def test_cloud_obscured_readings_lower_confidence():
    b = _baselines()
    clear = assess(AOI, date(2026, 5, 1),
                   {"truck_count": MetricReading("truck_count", 95.0, quality=1.0)}, b)
    murky = assess(AOI, date(2026, 5, 1),
                   {"truck_count": MetricReading("truck_count", 95.0, quality=0.3)}, b)
    assert murky.finding.confidence < clear.finding.confidence
    assert murky.finding.score == clear.finding.score


def test_unreportable_metrics_are_skipped_with_a_reason():
    a = assess(AOI, date(2026, 5, 1),
               {"truck_count": MetricReading(
                   "truck_count", 95.0, reportable=False,
                   caveat="10 m cannot resolve trucks")},
               _baselines())
    assert a.finding.score == 0.0
    assert "10 m cannot resolve trucks" in a.skipped["truck_count"]


def test_a_redrawn_boundary_invalidates_the_baseline():
    """History over a different footprint is not comparable, and saying so beats
    quietly carrying it forward."""
    stale = build_all(weekly_history(), date(2026, 5, 1), "a-different-fingerprint")
    a = assess(AOI, date(2026, 5, 1),
               {"truck_count": MetricReading("truck_count", 95.0)}, stale)
    assert a.finding.score == 0.0
    assert "boundary has changed" in a.skipped["truck_count"]


def test_headline_never_asserts_intent():
    a = assess(AOI, date(2026, 5, 1),
               {"truck_count": MetricReading("truck_count", 200.0)}, _baselines())
    text = headline(a).lower()
    assert "deviation" in text
    for word in ("threat", "hostile", "enemy", "attack", "intent"):
        assert word not in text


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------

def _event(kind=ChangeType.NEW_STRUCTURE, area=20_000.0, sev=Severity.HIGH,
           conf=0.9, ident="c1"):
    return ChangeEvent(
        id=ident, aoi_id=AOI.id, change_type=kind,
        detected_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        before_scene_id="b", after_scene_id="a",
        geometry=geo.rectangle((70.0, 23.0), 200, 100), area_m2=area,
        confidence=conf, magnitude=0.2, severity=sev)


def test_novelty_is_one_when_nothing_comparable_is_on_record():
    assert risk.score_change(_event(), AOI).novelty == 1.0


def test_novelty_falls_when_the_site_has_seen_it_before():
    history = [_event(ident=f"h{i}", area=19_000.0) for i in range(3)]
    assert risk.score_change(_event(), AOI, history=history).novelty < 0.2


def test_a_single_look_holds_an_ambiguous_finding_back():
    """Containers and a new building look identical in one pair at 10 m."""
    event = _event(sev=Severity.MEDIUM)
    score = risk.score_change(event, AOI, history=[_event(ident="h1")])
    assert risk.held_for_confirmation(event, score)


def test_novelty_alone_does_not_release_a_small_finding():
    """On empty terrain every speckle artefact is novel; that is not evidence."""
    small = _event(sev=Severity.HIGH, area=2_400.0)
    score = risk.score_change(small, AOI)          # no history, so novelty 1.0
    assert score.novelty == 1.0
    assert risk.held_for_confirmation(small, score)


def test_a_large_novel_structure_is_escalated_on_one_look():
    big = _event(sev=Severity.HIGH, area=200_000.0)
    score = risk.score_change(big, AOI)
    assert score.novelty == 1.0
    assert risk.held_for_confirmation(big, score) == ""


def test_confirmation_by_other_comparisons_releases_the_hold():
    """Measured backwards, because a pipeline running day by day has no future."""
    event = _event(sev=Severity.MEDIUM)
    others = [_event(ident="o1"), _event(ident="o2")]
    others[0].after_scene_id = "a1"
    others[1].after_scene_id = "a2"
    score = risk.score_change(event, AOI, history=[_event(ident="h1")],
                              others=others)
    assert score.persistence > 0.5
    assert score.looks == 3
    assert risk.held_for_confirmation(event, score) == ""


def test_persistence_ignores_the_comparison_the_finding_came_from():
    """Otherwise every finding confirms itself and the hold never means anything."""
    event = _event()
    same_look = _event(ident="s1")            # shares after_scene_id "a"
    score = risk.score_change(event, AOI, others=[same_look])
    assert score.looks == 1
    assert score.persistence == 0.0


def test_persistence_falls_when_other_looks_did_not_see_it():
    event = _event(sev=Severity.MEDIUM)
    elsewhere = []
    for i, scene in enumerate(("a1", "a2", "a3")):
        other = _event(ident=f"e{i}")
        other.after_scene_id = scene
        other.geometry = geo.rectangle((70.4, 23.4), 200, 100)   # 40 km away
        elsewhere.append(other)
    score = risk.score_change(event, AOI, others=elsewhere)
    assert score.looks == 4
    assert score.persistence == 0.0
    held = risk.held_for_confirmation(event, score)
    # Flagged once across four looks that all covered it is *better* evidence
    # of something moveable than flagged once with nothing to compare against.
    assert "did not show it" in held


def test_damage_is_never_held_back():
    event = _event(kind=ChangeType.POSSIBLE_DAMAGE, sev=Severity.CRITICAL)
    assert risk.held_for_confirmation(event, risk.score_change(event, AOI)) == ""


def test_confidence_scales_the_composite_within_its_severity_band():
    sure = risk.RiskScore(Severity.HIGH, 0.95, 0.5, 0.5, 0.01)
    unsure = risk.RiskScore(Severity.HIGH, 0.40, 0.5, 0.5, 0.01)
    assert sure.composite > unsure.composite
    assert unsure.composite < risk.RiskScore(
        Severity.CRITICAL, 0.95, 0.5, 0.5, 0.01).composite


def test_a_small_footprint_keeps_a_nonzero_spatial_share():
    """A building in a whole AOI is about one part in ten thousand.

    Rounded to three places that is exactly zero, and the console renders it
    as "0.00%" -- which an analyst reads as "none" for a change that is small
    but certainly there. The dimension has to survive serialisation.
    """
    score = risk.RiskScore(Severity.MEDIUM, 0.9, 0.0, 0.5, 0.00018)
    assert score.to_dict()["spatial"] > 0.0
    assert f"{score.to_dict()['spatial'] * 100:.2f}%" != "0.00%"

    text = " ".join(score.explain())
    assert "0.0%" not in text, text
    assert "tenth of a percent" in text

    #: Genuinely zero still says so, rather than implying a measurement.
    nothing = " ".join(risk.RiskScore(Severity.MEDIUM, 0.9, 0.0, 0.5, 0.0).explain())
    assert "too small to express" in nothing


def test_risk_explanation_names_every_dimension():
    text = " ".join(risk.RiskScore(Severity.HIGH, 0.9, 0.9, 0.0, 0.02).explain())
    for word in ("severity", "confidence", "novelty", "persistence", "spatial"):
        assert word in text


# ---------------------------------------------------------------------------
# Rules and the queue
# ---------------------------------------------------------------------------

def _facts(**over):
    base = {
        "finding_kind": "change", "change_type": "new_structure",
        "severity": "high", "severity_rank": 3, "confidence": 0.9,
        "area_m2": 20_000.0, "magnitude": 0.2, "aoi_id": AOI.id,
        "aoi_kind": AOI.kind.value, "novelty": 1.0, "persistence": 0.0,
        "looks": 1, "spatial_fraction": 0.01, "risk": 78.0,
        "place_bucket": "1:2", "title": "New structure", "summary": "x",
    }
    base.update(over)
    return base


def test_default_rules_fire_on_a_clear_new_structure():
    raised = alert_engine.evaluate(AOI, alert_engine.default_rules("org"),
                                   [_facts(area_m2=60_000.0)])
    assert raised
    assert raised[0].alert.priority == 1


def test_one_finding_raises_one_alert_however_many_rules_match():
    """A border-sector structure matches the general rule and the border rule."""
    rules = alert_engine.default_rules("org")
    facts = _facts(aoi_id=BORDER.id, aoi_kind="border_sector", area_m2=60_000.0)
    matching = [r for r in rules if r.applies_to(BORDER) and r.matches(facts)]
    assert len(matching) >= 2, "this test needs a finding two rules both match"

    raised = alert_engine.evaluate(BORDER, rules, [facts])
    assert len(raised) == 1
    assert {r.name for r in raised[0].rules} == {r.name for r in matching}
    # Attributed to the narrowest scope, and delivered on every channel asked for.
    assert raised[0].rule.aoi_kinds == ["border_sector"]
    assert "webhook" in raised[0].alert.delivered_to
    assert "email" in raised[0].alert.delivered_to


def test_the_shortest_suppression_window_among_matching_rules_wins():
    rules = alert_engine.default_rules("org")
    facts = _facts(aoi_id=BORDER.id, aoi_kind="border_sector", area_m2=60_000.0)
    at = datetime(2026, 5, 10, tzinfo=timezone.utc)
    first = alert_engine.evaluate(BORDER, rules, [facts], when=at)
    assert first
    key = first[0].dedup_key
    # The border rule suppresses for 3 days, the general one for 7. At 5 days
    # the border rule is free again, so the finding is allowed through.
    assert alert_engine.evaluate(BORDER, rules, [facts],
                                 when=at + timedelta(days=5),
                                 recent={key: at})
    assert alert_engine.evaluate(BORDER, rules, [facts],
                                 when=at + timedelta(days=2),
                                 recent={key: at}) == []


def test_a_rule_scoped_to_border_sectors_does_not_fire_at_a_port():
    border_rule = [r for r in alert_engine.default_rules("org")
                   if r.id == "rule-border-activity"]
    assert alert_engine.evaluate(AOI, border_rule, [_facts()]) == []
    fired = alert_engine.evaluate(
        BORDER, border_rule, [_facts(aoi_id=BORDER.id, aoi_kind="border_sector")])
    assert fired


def test_severity_floor_is_enforced():
    rules = [r for r in alert_engine.default_rules("org")
             if r.id == "rule-new-structure"]
    assert alert_engine.evaluate(
        AOI, rules, [_facts(severity="low", severity_rank=1)]) == []


def test_one_construction_site_produces_one_alert_not_forty():
    """Deduplication by place and kind, not by finding identity."""
    rules = alert_engine.default_rules("org")
    many = [_facts(place_bucket="7:7") for _ in range(40)]
    raised = alert_engine.evaluate(AOI, rules, many)
    assert len(raised) <= 2
    assert max(r.occurrences for r in raised) >= 20


def test_a_raised_alert_carries_the_key_it_was_deduplicated_under():
    """The pipeline stores this key; re-deriving it picked the wrong one."""
    rules = alert_engine.default_rules("org")
    raised = alert_engine.evaluate(AOI, rules, [_facts(place_bucket="4:9")])
    assert raised
    for r in raised:
        assert r.dedup_key
        assert r.dedup_key.startswith(f"{AOI.id}|")
        assert "4:9" in r.dedup_key
        assert r.dedup_key == alert_engine._dedup_key(
            AOI.id, _facts(place_bucket="4:9"))


def test_the_worst_finding_in_a_bucket_becomes_the_alert():
    """Never whichever the change engine happened to emit first."""
    rules = alert_engine.default_rules("org")
    minor = _facts(place_bucket="9:9", severity="medium", severity_rank=2,
                   risk=40.0, title="Minor", area_m2=60_000.0,
                   change_type="new_structure")
    major = _facts(place_bucket="9:9", severity="critical", severity_rank=4,
                   risk=95.0, title="Major", area_m2=400_000.0,
                   change_type="new_structure", change_event_id="c-major",
                   evidence_id="ev-major")

    for order in ([minor, major], [major, minor]):
        raised = alert_engine.evaluate(AOI, rules, order)
        assert len(raised) == 1, "one place and kind is one alert"
        assert raised[0].alert.title == "Major"
        assert raised[0].alert.severity is Severity.CRITICAL
        assert raised[0].alert.evidence_id == "ev-major"
        assert raised[0].occurrences == 2


def test_a_suppression_window_holds_across_runs():
    rules = [r for r in alert_engine.default_rules("org")
             if r.id == "rule-new-structure"]
    at = datetime(2026, 5, 10, tzinfo=timezone.utc)
    first = alert_engine.evaluate(AOI, rules, [_facts()], when=at)
    assert first
    key = first[0].dedup_key
    again = alert_engine.evaluate(AOI, rules, [_facts()],
                                  when=at + timedelta(days=2),
                                  recent={key: at})
    assert again == []
    later = alert_engine.evaluate(AOI, rules, [_facts()],
                                  when=at + timedelta(days=30),
                                  recent={key: at})
    assert later


def test_held_findings_never_take_the_top_of_the_queue():
    rules = alert_engine.default_rules("org")
    raised = alert_engine.evaluate(
        AOI, rules,
        [_facts(place_bucket="1:1", held_reason="seen once", risk=90.0,
                risk_score=risk.RiskScore(Severity.HIGH, 0.9, 1.0, 0.0, 0.01))])
    assert raised
    assert raised[0].alert.priority >= 3


def test_rule_description_is_readable_by_a_person():
    rule = alert_engine.default_rules("org")[0]
    text = rule.describe()
    assert text.startswith("IF ")
    assert "THEN alert via" in text
    # Field names are humanised; enum *values* keep their stored spelling so
    # that a rule shown in the interface still matches what is in the database.
    assert "finding kind" in text and "area m2" in text
    assert "finding_kind" not in text


def test_a_disabled_rule_never_fires():
    rules = alert_engine.default_rules("org")
    for r in rules:
        r.enabled = False
    assert alert_engine.evaluate(AOI, rules, [_facts()]) == []


def test_queue_summary_counts_bands():
    raised = alert_engine.evaluate(AOI, alert_engine.default_rules("org"),
                                   [_facts()])
    summary = alert_engine.queue_summary(raised)
    assert summary["total"] == len(raised)
    assert sum(summary[f"priority_{p}"] for p in (1, 2, 3, 4)) == len(raised)


def test_condition_operators_behave():
    from terrashield.alerts import Condition, Op
    facts = {"a": 5, "b": "x", "c": [1, 2]}
    assert Condition("a", Op.GTE, 5).holds(facts)
    assert not Condition("a", Op.GT, 5).holds(facts)
    assert Condition("b", Op.IN, ["x", "y"]).holds(facts)
    assert Condition("c", Op.CONTAINS, 2).holds(facts)
    assert not Condition("missing", Op.EQ, 1).holds(facts)
    # A type mismatch is False, never a crash mid-pipeline.
    assert not Condition("b", Op.GT, 5).holds(facts)


# ---------------------------------------------------------------------------
# Coverage of the enums
# ---------------------------------------------------------------------------
#
# These catch a specific and easy kind of drift: someone adds a member to an
# enum and wires it into one place. A change type the classifier can never
# produce is dead vocabulary in the interface; an object class with no detector
# profile is a class the product claims and cannot find; a rule whose condition
# names a value no enum has is a rule that silently never fires.

def test_every_change_type_is_reachable_from_the_classifier():
    import pathlib
    from terrashield.domain import ChangeType
    source = (pathlib.Path(__file__).resolve().parents[1]
              / "src" / "terrashield" / "change.py").read_text()
    missing = [t.name for t in ChangeType if f"ChangeType.{t.name}" not in source]
    assert missing == [], f"never produced by change.py: {missing}"


def test_every_object_class_has_a_detector_profile():
    from terrashield.detect import PROFILES
    from terrashield.domain import ObjectClass
    covered = {p.object_class for p in PROFILES}
    assert sorted(c.value for c in ObjectClass if c not in covered) == []


def test_every_default_rule_condition_names_a_real_value():
    """A rule naming a value no enum has is a rule that never fires."""
    from terrashield.domain import AoiKind, ChangeType, Severity
    known = ({e.value for e in ChangeType} | {e.value for e in AoiKind}
             | {e.value for e in Severity} | {"change", "anomaly"})
    for rule in alert_engine.default_rules("org"):
        for kind in rule.aoi_kinds:
            assert kind in {e.value for e in AoiKind}, rule.id
        for condition in rule.conditions:
            values = (condition.value if isinstance(condition.value, list)
                      else [condition.value])
            for value in values:
                if isinstance(value, str):
                    assert value in known, f"{rule.id}: {value!r}"


def test_every_tracked_class_is_either_scored_or_declared_structural():
    """A metric the pipeline records but the scorer ignores silently is a gap."""
    from terrashield.anomaly import STRUCTURAL_METRICS, WEIGHTS
    from terrashield.pipeline import TRACKED
    for klass in TRACKED:
        metric = f"{klass.value}_count"
        assert metric in WEIGHTS or metric in STRUCTURAL_METRICS, metric


# ---------------------------------------------------------------------------
# Trends
# ---------------------------------------------------------------------------

def _ramp(metric="truck_count", start_value=20.0, per_day=0.6, days=90,
          start=date(2026, 1, 1), noise=3.0):
    """History that climbs steadily, with no single day out of the ordinary.

    The day-to-day noise matters. Without it the only variation in the series
    *is* the ramp, so the robust scale is set by the ramp itself and the rise
    measures the same number of deviations whatever the slope — which makes
    the fixture insensitive to the thing it is meant to test. Real metrics
    wobble around a trend, and it is the wobble that sets the scale.
    """
    rng = Rng(seed_of(metric, "ramp"))
    return [Observation("AOI-T", metric, start + timedelta(days=i),
                        max(0.0, start_value + per_day * i
                            + rng.gauss(0.0, noise)))
            for i in range(days)]


def test_a_gradual_build_up_scores_even_though_no_day_stands_out():
    """The case a same-day deviation test cannot see, and the one
    pattern-of-life monitoring is sold on."""
    rows = _ramp()
    today = date(2026, 1, 1) + timedelta(days=90)
    baselines = build_all(rows, today, AOI.fingerprint)
    latest = rows[-1].value + 0.6

    a = assess(AOI, today, {"truck_count": MetricReading("truck_count", latest)},
               baselines, rows)
    assert a.trends["truck_count"] == pytest.approx(0.6, rel=0.2)
    #: The climb, not today's value, is what carries the score here.
    assert "truck_count:trend" in a.finding.contributions
    assert a.finding.score > 0
    assert any("has risen about" in r for r in a.finding.reasons)


def test_a_climb_nobody_could_see_in_one_day_says_so():
    """The phrase is earned only when today's value alone would not have scored."""
    rows = _ramp(per_day=1.2)
    today = date(2026, 1, 1) + timedelta(days=90)
    baselines = build_all(rows, today, AOI.fingerprint)
    median = baselines["truck_count"].stat_for(today).median

    a = assess(AOI, today,
               {"truck_count": MetricReading("truck_count", median)},
               baselines, rows)
    assert "truck_count" not in a.finding.contributions, "today is unremarkable"
    assert "truck_count:trend" in a.finding.contributions
    assert any("no single day in it stands out" in r for r in a.finding.reasons)


def test_a_flat_history_contributes_no_meaningful_trend():
    """Noise fits a slope of its own; it must not amount to anything."""
    rows = _ramp(per_day=0.0)
    today = date(2026, 1, 1) + timedelta(days=90)
    a = assess(AOI, today, {"truck_count": MetricReading("truck_count", 20.0)},
               build_all(rows, today, AOI.fingerprint), rows)
    assert a.finding.contributions.get("truck_count:trend", 0.0) < 0.08
    assert a.finding.score < 15


def test_a_declining_trend_is_not_an_anomaly():
    """A site getting quieter is not something to wake an analyst for."""
    rows = _ramp(start_value=80.0, per_day=-0.6)
    today = date(2026, 1, 1) + timedelta(days=90)
    a = assess(AOI, today, {"truck_count": MetricReading("truck_count", 26.0)},
               build_all(rows, today, AOI.fingerprint), rows)
    assert a.trends["truck_count"] < 0
    assert "truck_count:trend" not in a.finding.contributions


def test_a_metric_is_not_counted_twice_for_being_high_and_climbing():
    """One phenomenon seen two ways is one contribution, not two."""
    rows = _ramp()
    today = date(2026, 1, 1) + timedelta(days=90)
    baselines = build_all(rows, today, AOI.fingerprint)
    a = assess(AOI, today, {"truck_count": MetricReading("truck_count", 300.0)},
               baselines, rows)
    keys = set(a.finding.contributions)
    assert not ({"truck_count", "truck_count:trend"} <= keys), keys
    # The climb is still reported, just not scored a second time -- and as its
    # own sentence, because these lines are read as bullets in an evidence pack.
    followups = [r for r in a.finding.reasons if "risen about" in r]
    assert followups
    assert followups[0].startswith("It has also ")


def test_a_trend_contributes_less_than_a_same_day_excursion():
    rows = _ramp()
    today = date(2026, 1, 1) + timedelta(days=90)
    baselines = build_all(rows, today, AOI.fingerprint)
    trending = assess(AOI, today,
                      {"truck_count": MetricReading("truck_count", rows[-1].value)},
                      baselines, rows)
    spiking = assess(AOI, today,
                     {"truck_count": MetricReading("truck_count", 400.0)},
                     baselines, rows)
    assert spiking.finding.score > trending.finding.score


def test_a_steeper_climb_scores_higher_than_a_shallow_one():
    """Against realistic day-to-day noise, the slope has to matter."""
    today = date(2026, 1, 1) + timedelta(days=90)
    scores = []
    for per_day in (0.05, 0.30, 1.20):
        rows = _ramp(per_day=per_day)
        baselines = build_all(rows, today, AOI.fingerprint)
        median = baselines["truck_count"].stat_for(today).median
        a = assess(AOI, today,
                   {"truck_count": MetricReading("truck_count", median)},
                   baselines, rows)
        scores.append(a.finding.score)
    assert scores == sorted(scores), scores
    assert scores[0] < scores[-1]


def test_a_sustained_climb_can_reach_a_reportable_score_on_its_own():
    """A trend that cannot change an outcome would be decoration."""
    today = date(2026, 1, 1) + timedelta(days=90)
    rows = _ramp(per_day=1.2) + _ramp(metric="vessel_count", start_value=6.0,
                                      per_day=0.4, noise=1.2)
    baselines = build_all(rows, today, AOI.fingerprint)
    readings = {m: MetricReading(m, baselines[m].stat_for(today).median)
                for m in ("truck_count", "vessel_count")}
    a = assess(AOI, today, readings, baselines, rows)
    assert not any(k in a.finding.contributions
                   for k in ("truck_count", "vessel_count")), "no day stands out"
    assert a.finding.score >= 40, a.finding.contributions
    assert "deviation" in headline(a).lower()


def test_a_trend_driven_headline_reads_as_a_sentence():
    """The key stays machine-readable; the alert line should not."""
    rows = _ramp(per_day=1.2)
    today = date(2026, 1, 1) + timedelta(days=90)
    baselines = build_all(rows, today, AOI.fingerprint)
    median = baselines["truck_count"].stat_for(today).median
    a = assess(AOI, today, {"truck_count": MetricReading("truck_count", median)},
               baselines, rows)
    line = headline(a)
    assert "a sustained rise in truck count" in line
    assert ":trend" not in line
