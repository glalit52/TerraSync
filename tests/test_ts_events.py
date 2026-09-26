"""Fusion: many detections becoming the few events worth reading.

The number this module exists to move is alert noise. A construction site seen
on eleven passes produces eleven change records, and a queue built straight
from them shows the same excavation eleven times. These tests pin that the
grouping is correct, that it does not over-merge distinct things, and that an
event never states a purpose.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from terrashield.domain import ChangeEvent, ChangeType, Severity
from terrashield.events import (
    CONFIRMING_LOOKS, CONTINUITY_DAYS, SAME_PLACE_M, Event, EventKind,
    fuse, novelty, summarise,
)

AOI = "IN-KCH-SECTOR"
LON, LAT = 70.10, 23.90


def ch(ident, kind, day, lon=LON, lat=LAT, area=1000.0,
       sev=Severity.MEDIUM, conf=0.9, aoi=AOI, geometry=True):
    geom = None
    if geometry:
        geom = {"type": "Polygon", "coordinates": [[
            [lon, lat], [lon + 0.001, lat], [lon + 0.001, lat + 0.001],
            [lon, lat + 0.001], [lon, lat]]]}
    return ChangeEvent(
        id=f"chg-{ident}", aoi_id=aoi, change_type=kind,
        detected_at=datetime(2026, 7, 1, tzinfo=timezone.utc) + timedelta(days=day),
        before_scene_id="before", after_scene_id="after", geometry=geom,
        area_m2=area, confidence=conf, magnitude=0.4, severity=sev,
        explanation="measured", model_version="v1", evidence_id=f"ev-{ident}")


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def test_repeated_looks_at_one_site_become_one_event():
    """The whole point: eleven records of one excavation are one excavation."""
    findings = [ch(i, ChangeType.NEW_STRUCTURE, i * 4) for i in range(6)]
    events = fuse(findings)
    assert len(events) == 1
    assert events[0].looks == 6
    assert events[0].kind is EventKind.CONSTRUCTION


def test_distant_findings_stay_separate():
    near = ch(1, ChangeType.NEW_STRUCTURE, 0)
    far = ch(2, ChangeType.NEW_STRUCTURE, 4, lon=LON + 0.5, lat=LAT + 0.5)
    assert len(fuse([near, far])) == 2


def test_findings_just_inside_the_radius_merge():
    """A change mask's centroid moves as the footprint grows, so the radius has
    to tolerate that without merging genuinely separate sites."""
    a = ch(1, ChangeType.NEW_STRUCTURE, 0)
    b = ch(2, ChangeType.NEW_STRUCTURE, 4, lat=LAT + 0.0015)  # ~170 m
    assert len(fuse([a, b])) == 1


def test_a_long_quiet_gap_starts_a_new_episode():
    """A site quiet for longer than two chances to see it has stopped.
    Resuming later is a new episode, not a continuation."""
    first = ch(1, ChangeType.NEW_STRUCTURE, 0)
    later = ch(2, ChangeType.NEW_STRUCTURE, CONTINUITY_DAYS + 5)
    events = fuse([first, later])
    assert len(events) == 2, "beyond the continuity window"

    within = ch(3, ChangeType.NEW_STRUCTURE, CONTINUITY_DAYS - 2)
    assert len(fuse([first, within])) == 1, "inside it"


def test_areas_that_do_not_form_events_are_left_alone():
    """Object arrivals and departures are population movement, already counted.
    Filing each as an event recreates the noise fusion exists to remove."""
    movement = [ch(1, ChangeType.OBJECT_APPEARED, 0),
                ch(2, ChangeType.OBJECT_DEPARTED, 1)]
    assert fuse(movement) == []


def test_findings_from_different_areas_never_merge():
    a = ch(1, ChangeType.NEW_STRUCTURE, 0, aoi="IN-MUN-PORT")
    b = ch(2, ChangeType.NEW_STRUCTURE, 2, aoi="IN-BHD-SOLAR")
    assert len(fuse([a, b])) == 2


def test_a_finding_without_geometry_still_becomes_an_event():
    """A finding the analyst cannot see is worse than one that failed to merge."""
    events = fuse([ch(1, ChangeType.NEW_STRUCTURE, 0, geometry=False)])
    assert len(events) == 1
    assert events[0].looks == 1


# ---------------------------------------------------------------------------
# Sequences
# ---------------------------------------------------------------------------

def test_ground_disturbance_followed_by_structures_is_one_sequence():
    """Ground gets disturbed, then a building appears. One construction
    episode, not two unrelated events at the same coordinates."""
    events = fuse([ch(1, ChangeType.CONSTRUCTION_ACTIVITY, 0),
                   ch(2, ChangeType.NEW_STRUCTURE, 5)])
    assert len(events) == 1
    e = events[0]
    assert e.kind is EventKind.CONSTRUCTION, "the most developed stage names it"
    assert EventKind.GROUND_DISTURBANCE in e.composed_of, \
        "and the history is not silently relabelled"


def test_unrelated_kinds_do_not_merge_even_at_the_same_place():
    events = fuse([ch(1, ChangeType.NEW_STRUCTURE, 0),
                   ch(2, ChangeType.INUNDATION, 4)])
    assert len(events) == 2


# ---------------------------------------------------------------------------
# Measures
# ---------------------------------------------------------------------------

def test_severity_is_the_worst_look_not_an_average():
    """An event that looked critical once looked critical. Averaging it away
    buries a real finding under its own follow-ups."""
    events = fuse([ch(1, ChangeType.NEW_STRUCTURE, 0, sev=Severity.CRITICAL),
                   ch(2, ChangeType.NEW_STRUCTURE, 4, sev=Severity.LOW),
                   ch(3, ChangeType.NEW_STRUCTURE, 8, sev=Severity.LOW)])
    assert events[0].severity is Severity.CRITICAL


def test_repetition_raises_confidence_but_cannot_manufacture_certainty():
    once = fuse([ch(1, ChangeType.NEW_STRUCTURE, 0, conf=0.7)])[0]
    assert once.confidence == pytest.approx(0.7)

    many = fuse([ch(i, ChangeType.NEW_STRUCTURE, i * 3, conf=0.7)
                 for i in range(12)])[0]
    assert many.confidence > once.confidence, "a second look corroborates"
    assert many.confidence <= 0.8, "but weakly -- a systematic error repeats too"


def test_a_single_look_is_not_confirmed_and_says_so():
    e = fuse([ch(1, ChangeType.NEW_STRUCTURE, 0)])[0]
    assert e.looks < CONFIRMING_LOOKS
    assert not e.confirmed
    assert "transient artefact is not yet ruled out" in e.headline()


def test_trajectory_reports_growth_from_the_measured_areas():
    growing = fuse([ch(i, ChangeType.NEW_STRUCTURE, i * 3, area=500 + i * 500)
                    for i in range(5)])[0]
    assert growing.trajectory == "growing"

    shrinking = fuse([ch(i, ChangeType.NEW_STRUCTURE, i * 3, area=4000 - i * 700)
                      for i in range(5)])[0]
    assert shrinking.trajectory == "shrinking"

    steady = fuse([ch(i, ChangeType.NEW_STRUCTURE, i * 3, area=2000)
                   for i in range(5)])[0]
    assert steady.trajectory == "steady"


def test_duration_and_dates_come_from_the_findings():
    events = fuse([ch(i, ChangeType.NEW_STRUCTURE, i * 4) for i in range(4)])
    e = events[0]
    assert e.duration_days == 12
    assert e.started_on < e.last_seen_on


def test_evidence_ids_are_carried_so_the_chain_survives_fusion():
    """Grouping must not break the path back to the imagery."""
    events = fuse([ch(i, ChangeType.NEW_STRUCTURE, i * 3) for i in range(4)])
    payload = events[0].to_dict()
    assert len(payload["evidence_ids"]) == 4
    assert len(payload["finding_ids"]) == 4


# ---------------------------------------------------------------------------
# Identity and repeatability
# ---------------------------------------------------------------------------

def test_fusing_the_same_history_twice_gives_the_same_event_ids():
    """Otherwise every re-run produces a fresh wave of alerts for events the
    analyst already worked."""
    findings = [ch(i, ChangeType.NEW_STRUCTURE, i * 4) for i in range(5)]
    first = [e.id for e in fuse(findings)]
    second = [e.id for e in fuse(list(reversed(findings)))]
    assert first == second


# ---------------------------------------------------------------------------
# Novelty
# ---------------------------------------------------------------------------

def test_novelty_is_measured_against_the_site_not_the_world():
    """A new building at a construction site is routine. The same building on
    a salt flat is not."""
    busy = fuse([ch(i, ChangeType.NEW_STRUCTURE, i * 20) for i in range(4)])
    assert novelty(busy[-1], busy) < 0.5, "routine here"

    mixed = fuse([ch(1, ChangeType.INUNDATION, 0),
                  ch(2, ChangeType.INUNDATION, 20),
                  ch(3, ChangeType.NEW_STRUCTURE, 40)])
    structure = [e for e in mixed if e.kind is EventKind.CONSTRUCTION][0]
    assert novelty(structure, mixed) == 1.0, "never seen at this site before"


def test_the_first_event_at_a_site_is_wholly_novel():
    events = fuse([ch(1, ChangeType.NEW_STRUCTURE, 0)])
    assert novelty(events[0], events) == 1.0


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def test_the_summary_reports_the_noise_reduction():
    """The scope of work asks for this number per milestone."""
    findings = [ch(i, ChangeType.NEW_STRUCTURE, i * 3) for i in range(10)]
    stats = summarise(fuse(findings))
    assert stats["findings"] == 10
    assert stats["events"] == 1
    assert stats["reduction"] == pytest.approx(0.9)


def test_the_summary_separates_confirmed_from_single_look():
    findings = ([ch(i, ChangeType.NEW_STRUCTURE, i * 3) for i in range(4)]
                + [ch(90, ChangeType.INUNDATION, 0, lon=LON + 0.4)])
    stats = summarise(fuse(findings))
    assert stats["confirmed"] == 1
    assert stats["single_look"] == 1


def test_an_empty_history_summarises_without_dividing_by_zero():
    assert summarise([])["reduction"] == 0.0


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind,change", [
    (EventKind.CONSTRUCTION, ChangeType.NEW_STRUCTURE),
    (EventKind.DEMOLITION, ChangeType.STRUCTURE_REMOVED),
    (EventKind.WATER_EXTENT, ChangeType.INUNDATION),
    (EventKind.POSSIBLE_DAMAGE, ChangeType.POSSIBLE_DAMAGE),
    (EventKind.ACCESS_DEVELOPMENT, ChangeType.LINEAR_FEATURE),
])
def test_an_event_never_states_a_purpose(kind, change):
    """Same rule anomaly.py is held to: describe the ground, not a motive."""
    e = fuse([ch(1, change, 0), ch(2, change, 4)])[0]
    assert e.kind is kind
    text = (e.headline() + " " + e.to_dict()["headline"]).lower()
    for word in ("threat", "hostile", "enemy", "attack", "intent", "military",
                 "deliberate", "suspicious", "illicit"):
        assert word not in text, f"{word!r} asserts a purpose"
