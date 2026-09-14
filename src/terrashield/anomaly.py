"""Anomaly scoring: how far today is from this site's own history.

The scoring is the easy part. The discipline is in what the output is allowed
to say, and PRD section 55 is unambiguous: never "threat detected", always
"significant deviation from baseline", followed by what changed, where, when,
the evidence, the confidence, and why the model considers it unusual.

That is not squeamishness. A system that outputs intent is asserting something
it has no access to -- satellite imagery contains no intentions -- and the first
time it is confidently wrong in front of a customer, everything else it says
becomes suspect too. A system that outputs deviation is asserting something it
genuinely measured, and the analyst supplies the meaning. `AnomalyFinding`
therefore has a score, contributions and reasons, and no field anywhere in
which a motive could be recorded.

Confidence is separate from score on purpose. A large deviation measured
against eight observations through broken cloud is a big number that should not
be trusted, and collapsing the two into one figure is how that distinction gets
lost on the way to the dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from .baseline import (
    MIN_SAMPLES, TREND_WINDOW_DAYS, Baseline, Observation, trend,
)
from .domain import AnomalyFinding, Aoi, Severity
from .world import seed_of

MODEL_VERSION = "ts-anomaly-1.1.0"

#: How much each metric family contributes to the headline score. Structural
#: change outranks activity because a building is a fact and a vehicle count is
#: a snapshot -- the trucks may simply have been there when the satellite went
#: over.
WEIGHTS: dict[str, float] = {
    "vehicle_count": 0.55,
    "truck_count": 0.55,
    "construction_vehicle_count": 0.7,
    "vessel_count": 0.6,
    "aircraft_count": 0.7,
    "container_stack_count": 0.4,
    "object_arrivals": 0.5,
    "object_departures": 0.4,
}

DEFAULT_WEIGHT = 0.5

#: Counts that describe infrastructure rather than activity, and are therefore
#: excluded from the activity score.
#:
#: This is not squeamishness about a noisy metric, it is a statement about what
#: the number can mean. Buildings do not come and go between passes. A
#: building count that moves from four to seven has not recorded three new
#: buildings; it has recorded a detector finding three more things it was
#: willing to call a building, in different illumination, through a different
#: gap in the cloud. At 10 m that variation is larger than any real weekly
#: change in the built environment, so scoring it produces a stream of
#: confident, high-severity alerts about nothing -- which is precisely what the
#: first full run of the demo estate produced: every single priority-1 alert
#: across four sites was "activity deviation driven by building count".
#:
#: Real structural change is detected by comparing pixels, in change.py, which
#: is immune to this because it never counts anything. The counts are still
#: recorded and still shown on the site page; they just do not drive alerts.
STRUCTURAL_METRICS = frozenset({
    "building_count", "solar_array_count", "storage_tank_count",
    "tower_count", "road_count", "runway_count", "bridge_count",
    "detected_object_count",
})

#: Observations after which more history stops adding confidence.
FULL_HISTORY = 4 * MIN_SAMPLES

#: How much of a metric's weight a sustained trend can contribute, relative to
#: a same-day excursion.
#:
#: Nearly all of it, and the first attempt at 0.6 was wrong for a reason worth
#: recording: it capped a single trending metric at 33 out of 100, below the
#: threshold at which anything is even described as a deviation, so the
#: feature could never do anything except adjust a score that some other
#: metric had already raised. A capability that cannot change an outcome is
#: decoration.
#:
#: A sustained two-deviation rise across sixty days is not weaker evidence
#: than one unusual day -- it is arguably stronger, because a single day's
#: excursion can be one thin cloud or one busy Tuesday and a slope cannot. It
#: is discounted only because it says less about *today* specifically, which
#: is what an analyst is being asked to look at.
TREND_WEIGHT = 0.85

#: Rise over the trend window, in robust deviations, at which a trend
#: contributes its full share. Two means the metric has moved two deviations
#: across the window without any single day standing out.
TREND_SATURATION_Z = 2.0

#: Deviation at which a metric contributes its full weight. Three robust
#: deviations is roughly a one-in-a-hundred day for a well-behaved metric, and
#: saturating there stops a single wild value from pinning the whole score at
#: 100 and flattening the ranking between events.
SATURATION_Z = 4.0


@dataclass
class MetricReading:
    """Today's value of one metric, and what the sensor could see."""

    metric: str
    value: float
    quality: float = 1.0
    reportable: bool = True
    caveat: str = ""


@dataclass
class Assessment:
    """The full working of one anomaly assessment, not just its score."""

    finding: AnomalyFinding
    readings: dict[str, MetricReading] = field(default_factory=dict)
    deviations: dict[str, float] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    trends: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        f = self.finding
        return {
            "id": f.id,
            "aoi_id": f.aoi_id,
            "observed_at": f.observed_at.isoformat(),
            "score": round(f.score, 1),
            "confidence": round(f.confidence, 3),
            "severity": f.severity.value,
            "baseline_n": f.baseline_n,
            "reasons": list(f.reasons),
            "contributions": {k: round(v, 3) for k, v in sorted(f.contributions.items())},
            "deviations": {k: round(v, 2) for k, v in sorted(self.deviations.items())},
            "trends_per_day": {k: round(v, 4) for k, v in sorted(self.trends.items())},
            "skipped_metrics": dict(sorted(self.skipped.items())),
            "model_version": MODEL_VERSION,
        }


def assess(aoi: Aoi, when: date, readings: dict[str, MetricReading],
           baselines: dict[str, Baseline],
           history: list[Observation] | None = None,
           observed_at: datetime | None = None) -> Assessment:
    """Score one day at one AOI against that AOI's own history."""
    at = observed_at or datetime.combine(when, datetime.min.time(), timezone.utc)
    contributions: dict[str, float] = {}
    deviations: dict[str, float] = {}
    reasons: list[str] = []
    skipped: dict[str, str] = {}
    trends: dict[str, float] = {}
    baseline_ns: list[int] = []
    qualities: list[float] = []

    for metric, reading in sorted(readings.items()):
        if not reading.reportable:
            skipped[metric] = reading.caveat or (
                "the sensor used cannot resolve this class, so its count is "
                "not comparable with the baseline")
            continue
        if metric in STRUCTURAL_METRICS:
            skipped[metric] = (
                "infrastructure counts are not an activity signal: between two "
                "passes their variation is detector behaviour, not change on "
                "the ground. Structural change is detected by comparing pixels "
                "rather than by counting")
            continue
        base = baselines.get(metric)
        if base is None:
            skipped[metric] = "no baseline yet for this metric at this AOI"
            continue
        if base.aoi_fingerprint and base.aoi_fingerprint != aoi.fingerprint:
            skipped[metric] = (
                "the AOI boundary has changed since this baseline was built; "
                "history over a different footprint is not comparable")
            continue

        z = base.deviation(when, reading.value)
        deviations[metric] = z
        baseline_ns.append(base.n)
        qualities.append(reading.quality)

        weight = WEIGHTS.get(metric, DEFAULT_WEIGHT)
        #: Only excursions above normal contribute. A quiet day at a port is a
        #: quiet day, and scoring it as anomalous fills the queue with Sundays.
        today_share = weight * min(1.0, max(0.0, z) / SATURATION_Z)

        trend_share, trend_reason, trend_followup = 0.0, "", ""
        if history:
            slope = trend(history, metric, when)
            if abs(slope) > 1e-9:
                trends[metric] = slope
                #: The rise the slope implies across the whole window, scored
                #: in the same robust units as a same-day deviation. Only
                #: upward, for the same reason excursions are: a site getting
                #: quieter is not something to wake an analyst for.
                rise = slope * TREND_WINDOW_DAYS
                if rise > 0:
                    stat = base.stat_for(when)
                    rise_z = stat.z(stat.median + rise)
                    trend_share = weight * TREND_WEIGHT * min(
                        1.0, max(0.0, rise_z) / TREND_SATURATION_Z)
                    #: Two phrasings, because these lines are read as
                    #: standalone bullets in an evidence pack. One leads a
                    #: finding; the other follows the same metric's own
                    #: excursion and must not repeat its name or open with
                    #: a conjunction.
                    climb = (f"risen about {rise:,.0f} over the preceding "
                             f"{TREND_WINDOW_DAYS} days ({rise_z:.1f} robust "
                             "deviations across the window)")
                    trend_reason = (
                        f"{metric.replace('_', ' ')} has {climb}"
                        + ("" if today_share > 0.01
                           else " — no single day in it stands out"))
                    trend_followup = f"It has also {climb}."

        #: The stronger of the two, not their sum. A metric that is high today
        #: *and* has been climbing is one phenomenon seen two ways, and adding
        #: both counts the same traffic twice -- which would make a metric with
        #: a trend term outrank a metric without one purely for having been
        #: measured for longer.
        if trend_share > today_share and trend_share > 0.01:
            contributions[f"{metric}:trend"] = trend_share
            reasons.append(trend_reason)
        elif today_share > 0.01:
            contributions[metric] = today_share
            reasons.append(base.describe(when, reading.value))
            if trend_followup:
                reasons.append(trend_followup)

    #: Combine as a soft maximum rather than a sum. A sum lets six mildly
    #: elevated metrics outscore one metric that is six deviations out, which
    #: inverts the ranking exactly where it matters most.
    if contributions:
        ordered = sorted(contributions.values(), reverse=True)
        combined = ordered[0]
        for extra in ordered[1:]:
            combined += extra * (1.0 - combined) * 0.55
        score = 100.0 * min(1.0, combined)
    else:
        score = 0.0

    n = min(baseline_ns) if baseline_ns else 0
    quality = sum(qualities) / len(qualities) if qualities else 0.0
    #: Confidence is about the *evidence*, never about the size of the
    #: deviation. Short history, poor visibility or few comparable metrics all
    #: reduce it, and a huge number measured badly stays a huge number measured
    #: badly rather than becoming a certainty.
    #: Multiplied, not summed. Each factor is an independent way for the
    #: evidence to be weak, and any one of them being weak should be able to
    #: pull the whole number down -- a sum lets good visibility and three
    #: metrics compensate for a baseline of eight observations, which is
    #: exactly the compensation that should not happen.
    #:
    #: `FULL_HISTORY` is where history stops adding confidence: four times the
    #: minimum, about a quarter of a year of Sentinel-2 looks. Reaching full
    #: confidence at twelve observations would call a fortnight's history a
    #: characterised site.
    history_term = min(1.0, n / FULL_HISTORY) if n else 0.0
    breadth = min(1.0, len(deviations) / 3.0)
    confidence = round((0.35 + 0.65 * history_term)
                       * (0.55 + 0.45 * quality)
                       * (0.75 + 0.25 * breadth), 3)

    if n < FULL_HISTORY and contributions:
        #: Warn wherever history is still shortening confidence, not only at
        #: the floor. A site with eight looks behind it is being compared with
        #: a fortnight of its own record, and the analyst should read the
        #: finding in that light rather than discover it from a small number in
        #: a confidence column.
        reasons.append(
            f"baseline rests on {n} prior observation(s), below the "
            f"{FULL_HISTORY} at which this site would be considered "
            "characterised; treat the comparison as provisional")

    finding = AnomalyFinding(
        id="anom-" + format(seed_of(aoi.id, when.isoformat()) & 0xFFFFFFFFFFFF, "012x"),
        aoi_id=aoi.id, observed_at=at, score=round(score, 1),
        confidence=min(0.95, confidence), reasons=reasons,
        contributions={k: round(v, 4) for k, v in contributions.items()},
        baseline_n=n,
    )
    return Assessment(finding=finding, readings=readings, deviations=deviations,
                      skipped=skipped, trends=trends)


def headline(assessment: Assessment) -> str:
    """One sentence for the alert list. Analytical, never attributive."""
    f = assessment.finding
    if f.score < 40:
        return "Within normal range for this site"
    top = sorted(f.contributions.items(), key=lambda kv: -kv[1])
    if not top:
        driver = "multiple metrics"
    else:
        key = top[0][0]
        #: A trend driver is stored as "metric:trend" so that the score's
        #: working stays machine-readable. A person reading an alert wants the
        #: sentence, not the key.
        driver = (f"a sustained rise in {key[:-6].replace('_', ' ')}"
                  if key.endswith(":trend") else key.replace("_", " "))
    band = {
        Severity.CRITICAL: "Large deviation",
        Severity.HIGH: "Significant deviation",
        Severity.MEDIUM: "Moderate deviation",
        Severity.LOW: "Minor deviation",
    }[f.severity]
    return (f"{band} from the site's historical baseline, driven by {driver} "
            f"(score {f.score:.0f}/100, confidence {f.confidence:.0%})")
