"""The monitoring run: catalogue to alert, once per AOI per day.

This is the loop PRD section 33 describes, and its job is less to be clever
than to be *repeatable*. A monitoring platform is judged on whether it says the
same thing twice about the same day, so every stage is idempotent: re-running a
date range rewrites the same rows rather than duplicating them, because ids are
derived from content rather than from a counter.

The sequence:

    search        what imagery exists, usable and not
    fetch         pixels, plus the provider's cloud and water bands
    detect        objects, with the classes the sensor cannot support declared
    observe       today's metrics, weighted by how much of the AOI was visible
    compare       change against the best legitimate earlier scene
    baseline      normal, from this AOI's own history before today
    assess        deviation from that normal, with its working
    risk          severity, confidence, novelty, persistence, spatial reach
    evidence      what each finding rests on, hashed
    alert         rules, deduplication, suppression, ranking

Two things are deliberately not here. There is no stage that decides what a
change *means* -- that is the analyst's, and the schema has nowhere to put it.
And there is no stage that silently skips a day: a day with no usable imagery
produces a recorded gap, because an empty timeline and an unobserved one look
identical on a chart and are completely different in a briefing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from . import alerts as alert_engine
from . import change, detect, evidence, risk
from .anomaly import Assessment, MetricReading, assess, headline
from .baseline import Observation, build_all
from .catalog import Coverage, Provider, coverage
from .domain import Aoi, ChangeEvent, ObjectClass, Scene, Sensor, SiteStatus
from .store.repo import Store

#: Object classes whose counts are tracked as pattern-of-life metrics.
TRACKED = (
    ObjectClass.VEHICLE, ObjectClass.TRUCK, ObjectClass.CONSTRUCTION_VEHICLE,
    ObjectClass.VESSEL, ObjectClass.AIRCRAFT, ObjectClass.CONTAINER_STACK,
    ObjectClass.BUILDING,
)

#: How far back to look for the "before" half of a change pair. Long enough to
#: find a clear scene through a monsoon, short enough that the change is still
#: located usefully in time.
LOOKBACK_DAYS = 60

#: A site with no usable imagery for longer than this is reported as STALE.
#: Not an alert -- an absence of evidence is not evidence -- but visible, because
#: "we have not seen this place for three weeks" is itself worth knowing.
STALE_AFTER_DAYS = 21


@dataclass
class RunResult:
    """What one AOI-day produced, including the reasons it produced nothing."""

    aoi_id: str
    when: date
    scenes_found: int = 0
    scenes_usable: int = 0
    scene_used: str = ""
    detections: int = 0
    change_events: int = 0
    alerts: int = 0
    anomaly_score: float = 0.0
    anomaly_confidence: float = 0.0
    status: SiteStatus = SiteStatus.NORMAL
    notes: list[str] = field(default_factory=list)
    coverage: Coverage | None = None
    assessment: Assessment | None = None
    change_result: change.ChangeResult | None = None
    raised: list[alert_engine.RaisedAlert] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "aoi_id": self.aoi_id,
            "date": self.when.isoformat(),
            "scenes_found": self.scenes_found,
            "scenes_usable": self.scenes_usable,
            "scene_used": self.scene_used,
            "detections": self.detections,
            "change_events": self.change_events,
            "alerts": self.alerts,
            "anomaly_score": round(self.anomaly_score, 1),
            "anomaly_confidence": round(self.anomaly_confidence, 3),
            "status": self.status.value,
            "notes": list(self.notes),
            "coverage": self.coverage.to_dict() if self.coverage else None,
        }


def run_day(store: Store, provider: Provider, aoi: Aoi, when: date,
            rules: list | None = None,
            lookback_days: int = LOOKBACK_DAYS) -> RunResult:
    """Monitor one AOI for one day."""
    result = RunResult(aoi_id=aoi.id, when=when)
    window_start = when - timedelta(days=lookback_days)

    scenes = provider.search(aoi, window_start, when)
    store.put_scenes(scenes)
    cov = coverage(aoi.id, scenes, window_start, when)
    result.coverage = cov
    result.scenes_found = cov.acquired
    result.scenes_usable = cov.usable

    today = [s for s in scenes if s.acquired_on == when and s.usable]
    if not today:
        rejected = [s for s in scenes if s.acquired_on == when and not s.usable]
        if rejected:
            result.notes.append(
                "imagery was acquired today but is not usable: "
                + "; ".join(sorted({s.unusable_reason for s in rejected})))
        else:
            result.notes.append("no acquisition over this area today")
        if cov.last_usable is None or (when - cov.last_usable).days > STALE_AFTER_DAYS:
            result.status = SiteStatus.STALE
            result.notes.append(
                f"no usable imagery for {(when - cov.last_usable).days} days"
                if cov.last_usable else
                "no usable imagery anywhere in the lookback window")
        return result

    #: Prefer optical when it is available -- more classes are resolvable -- and
    #: fall back to SAR, which is the whole reason SAR is in the constellation
    #: mix. At a monsoon site that fallback is most of the year.
    today.sort(key=lambda s: (s.sensor is not Sensor.OPTICAL, -s.gsd_m))
    scene = today[0]
    result.scene_used = scene.id

    raster = provider.fetch(aoi, scene)
    store.record_imagery_access(scene.id, aoi.id)
    masks = provider.masks(aoi, scene, raster)

    det = detect.detect(aoi, scene, raster, obscured=masks.cloud, water=masks.water)
    store.put_detections(det.detections)
    result.detections = len(det.detections)
    visible = 1.0 - det.obscured_fraction
    if det.obscured_fraction > 0.05:
        result.notes.append(
            f"{det.obscured_fraction:.0%} of the area was under cloud; counts "
            "are over the visible remainder only")

    observations = _observations(aoi, scene, det, when, visible)

    #: The comparison runs before the assessment, not after it. Arrivals and
    #: departures are metrics like any other, and appending them afterwards
    #: left them stored for tomorrow's baseline but missing from today's
    #: readings -- so the two weights they carry could never fire, and a port
    #: that turned over three times its usual traffic scored on vessel count
    #: alone.
    change_result, before_scene = _compare_against_history(
        store, provider, aoi, scene, scenes, raster, masks)
    if change_result and change_result.usable:
        #: Counted rather than filed as change events: that is what keeps a
        #: busy port's change record readable, and it gives the baseline engine
        #: two metrics a single-scene object count cannot produce -- how much
        #: traffic turned over between looks, in each direction.
        observations.append(Observation(
            aoi.id, "object_arrivals", when, float(change_result.arrivals),
            quality=visible, scene_id=scene.id))
        observations.append(Observation(
            aoi.id, "object_departures", when, float(change_result.departures),
            quality=visible, scene_id=scene.id))
        if change_result.movements:
            result.notes.append(
                f"{change_result.arrivals} object(s) arrived and "
                f"{change_result.departures} left between the compared scenes; "
                "counted as activity, not filed as change")
    store.put_observations(observations)

    history = store.list_observations(aoi.id, end=when - timedelta(days=1))
    baselines = build_all(history, when, aoi.fingerprint)

    readings = {
        o.metric: MetricReading(
            o.metric, o.value, quality=visible,
            reportable=_reportable(det, o.metric),
            caveat=_caveat(det, o.metric))
        for o in observations
    }
    assessment = assess(aoi, when, readings, baselines, history, scene.acquired_at)
    store.put_anomaly(assessment.finding)
    result.assessment = assessment
    result.anomaly_score = assessment.finding.score
    result.anomaly_confidence = assessment.finding.confidence

    anomaly_bundle = evidence.for_anomaly(
        assessment.finding, aoi, assessment, [scene], baselines)
    store.put_evidence(anomaly_bundle)
    assessment.finding.evidence_id = anomaly_bundle.id
    store.put_anomaly(assessment.finding)

    result.change_result = change_result
    if change_result and not change_result.usable:
        result.notes.append("change comparison refused: " + change_result.refusal)
    if change_result and change_result.usable and before_scene is not None:
        #: Evidence first, then persist: each event carries the id of the
        #: bundle that justifies it, so a stored finding is never without one.
        for event in change_result.events:
            bundle = evidence.for_change(event, aoi, before_scene, scene,
                                         change_result, baselines)
            store.put_evidence(bundle)
            event.evidence_id = bundle.id
        store.put_changes(change_result.events)
        result.change_events = len(change_result.events)

    facts = _facts(store, aoi, change_result, assessment, when)

    rules = rules if rules is not None else store.list_rules()
    raised = alert_engine.evaluate(
        aoi, rules, facts, when=scene.acquired_at,
        recent=store.last_alert_times())
    for r in raised:
        store.put_alert(r.alert, r.to_dict(), dedup_key=r.dedup_key,
                        occurrences=r.occurrences)
    result.raised = raised
    result.alerts = len(raised)
    result.status = _status(assessment.finding.score, raised)
    return result


def _observations(aoi: Aoi, scene: Scene, det: detect.DetectionResult,
                  when: date, visible: float) -> list[Observation]:
    rows = [
        Observation(aoi.id, f"{klass.value}_count", when,
                    float(det.count_of(klass)), quality=visible,
                    scene_id=scene.id)
        for klass in TRACKED
    ]
    rows.append(Observation(aoi.id, "detected_object_count", when,
                            float(len(det.detections)), quality=visible,
                            scene_id=scene.id))
    return rows


def _reportable(det: detect.DetectionResult, metric: str) -> bool:
    if not metric.endswith("_count") or metric == "detected_object_count":
        return True
    name = metric[: -len("_count")]
    try:
        return det.reportable(ObjectClass(name))
    except ValueError:
        return True


def _caveat(det: detect.DetectionResult, metric: str) -> str:
    if not metric.endswith("_count"):
        return ""
    name = metric[: -len("_count")]
    try:
        return det.caveat(ObjectClass(name))
    except ValueError:
        return ""


def _compare_against_history(
        store: Store, provider: Provider, aoi: Aoi, scene: Scene,
        scenes: list[Scene], raster, masks
) -> tuple[change.ChangeResult | None, Scene | None]:
    """Difference today against the best legitimate earlier scene.

    "Legitimate" is doing real work: same constellation, and for SAR the same
    ground track. `best_pair` enforces it, and when no valid partner exists the
    honest answer is no comparison rather than a comparison across sensors that
    would light up the whole AOI.
    """
    candidates = [s for s in scenes
                  if s.constellation is scene.constellation and s.usable
                  and s.acquired_on < scene.acquired_on
                  and (scene.sensor is not Sensor.SAR or s.orbit == scene.orbit)]
    if not candidates:
        return None, None
    before = max(candidates, key=lambda s: s.acquired_at)
    before_raster = provider.fetch(aoi, before)
    store.record_imagery_access(before.id, aoi.id)
    before_masks = provider.masks(aoi, before, before_raster)
    return change.compare(aoi, before, scene, before_raster, raster,
                          before_masks.cloud, masks.cloud,
                          before_masks.water, masks.water), before


def _facts(store: Store, aoi: Aoi, change_result, assessment: Assessment,
           when: date) -> list[dict]:
    """Turn findings into the flat dictionaries rules are written against."""
    facts: list[dict] = []
    history = store.list_changes(aoi.id, end=when - timedelta(days=1), limit=400)

    if change_result and change_result.usable:
        for event in change_result.events:
            #: `history` answers both questions the score needs: what this
            #: site has looked like before (novelty) and how many earlier
            #: comparisons covered this same place (persistence). Passing an
            #: empty forward window instead left every finding permanently
            #: unconfirmed, so nothing was ever released from the hold.
            score = risk.score_change(event, aoi, history=history)
            held = risk.held_for_confirmation(event, score)
            f = alert_engine.change_facts(event, aoi, score)
            lon, lat = event.centroid
            f["place_bucket"] = alert_engine.place_bucket(lon, lat)
            f["change_event_id"] = event.id
            f["evidence_id"] = event.evidence_id
            f["risk_score"] = score
            f["held_reason"] = held
            f["title"] = _change_title(event)
            f["summary"] = event.explanation
            facts.append(f)

    finding = assessment.finding
    if finding.score > 0:
        f = alert_engine.anomaly_facts(finding, aoi)
        f["place_bucket"] = alert_engine.place_bucket(*aoi.centroid)
        f["anomaly_id"] = finding.id
        f["evidence_id"] = finding.evidence_id
        f["title"] = "Activity deviation from baseline"
        f["summary"] = headline(assessment)
        facts.append(f)
    return facts


def _change_title(event: ChangeEvent) -> str:
    words = event.change_type.value.replace("_", " ")
    return f"{words[0].upper()}{words[1:]} — {event.area_m2:,.0f} m2"


def _status(anomaly_score: float, raised: list) -> SiteStatus:
    if any(r.alert.priority <= 2 for r in raised) or anomaly_score >= 65:
        return SiteStatus.ANOMALOUS
    if raised or anomaly_score >= 40:
        return SiteStatus.WATCH
    return SiteStatus.NORMAL


def run_range(store: Store, provider: Provider, aoi: Aoi, start: date, end: date,
              rules: list | None = None) -> list[RunResult]:
    """Monitor one AOI across a date range, oldest day first.

    Oldest first matters: each day's baseline is built from the days before it,
    so running backwards would assess every day against a future it has not
    seen yet.
    """
    out = []
    day = start
    while day <= end:
        out.append(run_day(store, provider, aoi, day, rules))
        day += timedelta(days=1)
    return out


def health(store: Store) -> dict:
    intact, bad = store.verify_audit_chain()
    return {
        "status": "ok" if intact else "audit chain broken",
        "database": store.path,
        "schema_version": store.version,
        "organisation": store.org_id,
        "counts": store.counts(),
        "audit_chain_intact": intact,
        "first_bad_audit_entry": bad,
    }
