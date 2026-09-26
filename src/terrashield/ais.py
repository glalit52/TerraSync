"""AIS, and correlating it with what the imagery saw.

The maritime module's reason to exist is the discrepancy: a vessel the radar
sees and the transponder does not. That is a real and useful signal, and it is
also the single easiest place in this product to say something defamatory about
a named ship and its owner.

So the vocabulary here is deliberate and enforced. A detection with no matching
AIS report is **uncorrelated**. It is not dark, not going dark, not evading,
not illicit, not suspicious. The reasons a legitimate vessel appears
uncorrelated are ordinary and numerous:

* Class B transponders are low-power and terrestrial receivers miss them
  routinely beyond about 20 nautical miles;
* satellite AIS has gaps in coverage and latency measured in tens of minutes;
* fishing vessels under many flags are not required to carry AIS at all;
* transponders fail, and crews switch them off for legitimate safety reasons
  in piracy-risk waters;
* the SAR detection may not be a vessel -- a wind-driven wave facet, a fixed
  platform or a navigation buoy all return bright.

Every one of those produces exactly the signature that a deliberately silent
vessel produces. The imagery cannot tell them apart, so the system does not
try. It reports that a detection did not correlate, states the search window it
used, and leaves the inference to an analyst who knows the sea area.

`Correlation.headline()` is tested against the vocabulary of accusation the
same way `anomaly.py` and `events.py` are.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from .geo import haversine_m

#: How far either side of an image's timestamp to look for a position report.
#: Terrestrial AIS updates every few seconds for a moving vessel, but satellite
#: AIS revisit is far coarser, and a report thirty minutes stale is still the
#: best evidence available about where a ship was.
DEFAULT_WINDOW = timedelta(minutes=30)

#: How far a reported position may sit from a detection and still be the same
#: vessel. A ship at 20 knots covers about 600 m a minute, so this has to scale
#: with the age of the report rather than being one fixed radius -- which is
#: exactly what `_reach_m` does. This is the floor, covering geolocation error
#: in the image and position error in the report.
BASE_TOLERANCE_M = 500.0

#: Assumed speed when a report carries none. Fifteen knots is an unremarkable
#: merchant transit; assuming zero would make stale reports impossible to match
#: and manufacture uncorrelated detections out of ordinary latency.
ASSUMED_SPEED_KTS = 15.0
KTS_TO_MS = 0.514444


class Correlated(str, Enum):
    MATCHED = "matched"                # a report places a vessel here, then
    UNCORRELATED = "uncorrelated"      # no report did -- which is not a verdict
    NO_COVERAGE = "no_coverage"        # no AIS at all for this time and place


@dataclass(frozen=True)
class AisReport:
    """One position report. A claim by a transponder, not ground truth."""

    mmsi: str
    at: datetime
    lon: float
    lat: float
    name: str = ""
    speed_kts: float | None = None
    course_deg: float | None = None
    #: Terrestrial receivers have hard range limits; satellite AIS does not but
    #: has coverage gaps. Which one saw it changes how much a gap means.
    source: str = "terrestrial"

    def to_dict(self) -> dict:
        return {"mmsi": self.mmsi, "at": self.at.isoformat(),
                "lon": round(self.lon, 6), "lat": round(self.lat, 6),
                "name": self.name, "speed_kts": self.speed_kts,
                "source": self.source}


@dataclass(frozen=True)
class VesselDetection:
    """A vessel-like object found in imagery. Possibly not a vessel."""

    id: str
    aoi_id: str
    at: datetime
    lon: float
    lat: float
    length_m: float = 0.0
    confidence: float = 0.0
    sensor: str = "sar"

    def to_dict(self) -> dict:
        return {"id": self.id, "aoi_id": self.aoi_id, "at": self.at.isoformat(),
                "lon": round(self.lon, 6), "lat": round(self.lat, 6),
                "length_m": round(self.length_m, 1),
                "confidence": round(self.confidence, 3), "sensor": self.sensor}


@dataclass
class Correlation:
    """What the transponder record says about one detection."""

    detection: VesselDetection
    status: Correlated
    report: AisReport | None = None
    distance_m: float | None = None
    lag_s: float | None = None
    window: timedelta = DEFAULT_WINDOW
    reports_in_window: int = 0
    #: Every reason a legitimate vessel looks uncorrelated. Carried on the
    #: finding rather than left in documentation, so it reaches the analyst
    #: with the finding instead of being something they have to remember.
    caveats: list[str] = field(default_factory=list)

    @property
    def matched(self) -> bool:
        return self.status is Correlated.MATCHED

    def headline(self) -> str:
        det = self.detection
        where = f"{det.lat:.4f}, {det.lon:.4f}"
        if self.status is Correlated.MATCHED and self.report is not None:
            who = self.report.name or f"MMSI {self.report.mmsi}"
            return (f"vessel detection at {where} correlates with {who}, "
                    f"{self.distance_m:,.0f} m and {self.lag_s / 60:.0f} min "
                    f"from its reported position")
        if self.status is Correlated.NO_COVERAGE:
            return (f"vessel detection at {where} has no AIS coverage for this "
                    f"time and place, so correlation could not be attempted")
        return (f"vessel detection at {where} did not correlate with any of "
                f"{self.reports_in_window} AIS report(s) within "
                f"{int(self.window.total_seconds() / 60)} minutes. This is an "
                f"absence of correlation, not evidence about the vessel")

    def to_dict(self) -> dict:
        return {
            "detection": self.detection.to_dict(),
            "status": self.status.value,
            "matched": self.matched,
            "report": self.report.to_dict() if self.report else None,
            "distance_m": (round(self.distance_m, 1)
                           if self.distance_m is not None else None),
            "lag_s": round(self.lag_s) if self.lag_s is not None else None,
            "reports_in_window": self.reports_in_window,
            "window_minutes": int(self.window.total_seconds() / 60),
            "caveats": self.caveats,
            "headline": self.headline(),
        }


#: Why a legitimate vessel shows no correlation. Attached to every
#: uncorrelated finding.
UNCORRELATED_CAVEATS = [
    "Class B transponders are low-power and terrestrial receivers miss them "
    "routinely beyond roughly 20 nautical miles",
    "satellite AIS has coverage gaps and latency of tens of minutes",
    "many fishing vessels are not required to carry AIS at all",
    "transponders fail, and are switched off for legitimate safety reasons in "
    "some waters",
    "the detection may not be a vessel: wave facets, fixed platforms and "
    "navigation buoys all return bright on SAR",
]


def _reach_m(report: AisReport, lag_s: float) -> float:
    """How far this vessel could have travelled in the gap.

    A fixed radius is wrong in both directions: too tight for a stale report on
    a fast ship, and too loose for a fresh one, which merges neighbouring
    vessels in a crowded anchorage.
    """
    speed = report.speed_kts if report.speed_kts is not None else ASSUMED_SPEED_KTS
    return BASE_TOLERANCE_M + abs(speed) * KTS_TO_MS * abs(lag_s)


def correlate(detection: VesselDetection, reports: list[AisReport],
              window: timedelta = DEFAULT_WINDOW,
              has_coverage: bool = True) -> Correlation:
    """Match one detection against the transponder record.

    Returns the closest report that could plausibly be the same vessel, or an
    honest statement that none did.
    """
    if not has_coverage:
        return Correlation(detection, Correlated.NO_COVERAGE, window=window,
                           caveats=["no AIS feed covers this time and place"])

    in_window = [r for r in reports
                 if abs((r.at - detection.at).total_seconds())
                 <= window.total_seconds()]

    best: tuple[float, AisReport, float] | None = None
    for report in in_window:
        lag = abs((report.at - detection.at).total_seconds())
        distance = haversine_m((detection.lon, detection.lat),
                               (report.lon, report.lat))
        if distance > _reach_m(report, lag):
            continue
        #: Closest in space wins, with time as the tie-break. Two reports from
        #: the same vessel seconds apart should not produce different answers.
        if best is None or (distance, lag) < (best[0], best[2]):
            best = (distance, report, lag)

    if best is None:
        return Correlation(detection, Correlated.UNCORRELATED, window=window,
                           reports_in_window=len(in_window),
                           caveats=list(UNCORRELATED_CAVEATS))
    distance, report, lag = best
    return Correlation(detection, Correlated.MATCHED, report=report,
                       distance_m=distance, lag_s=lag, window=window,
                       reports_in_window=len(in_window))


def correlate_all(detections: list[VesselDetection], reports: list[AisReport],
                  window: timedelta = DEFAULT_WINDOW,
                  has_coverage: bool = True) -> list[Correlation]:
    """Correlate a scene's worth of detections.

    A report already claimed by a closer detection is not offered to a second
    one: two ships in an anchorage must not both match the same transponder.
    """
    out: list[Correlation] = []
    claimed: set[int] = set()
    ordered = sorted(detections, key=lambda d: -d.confidence)
    for detection in ordered:
        available = [r for i, r in enumerate(reports) if i not in claimed]
        result = correlate(detection, available, window, has_coverage)
        if result.report is not None:
            for i, r in enumerate(reports):
                if i not in claimed and r is result.report:
                    claimed.add(i)
                    break
        out.append(result)
    return out


def summarise(correlations: list[Correlation]) -> dict:
    """Counts for the maritime view, worded so the total cannot be misread."""
    matched = [c for c in correlations if c.status is Correlated.MATCHED]
    uncorrelated = [c for c in correlations
                    if c.status is Correlated.UNCORRELATED]
    no_coverage = [c for c in correlations
                   if c.status is Correlated.NO_COVERAGE]
    return {
        "detections": len(correlations),
        "matched": len(matched),
        "uncorrelated": len(uncorrelated),
        "no_coverage": len(no_coverage),
        "note": ("uncorrelated means no AIS report was found for a detection "
                 "in the search window. It is not a finding about any vessel's "
                 "conduct, and the reasons a legitimate vessel appears this "
                 "way are ordinary and numerous"),
    }
