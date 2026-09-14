"""Scoring the engines against known truth.

PRD section 54 asks for detection precision, recall, false-positive rate and
confidence calibration. This module computes them, and it is part of the
product rather than part of the test suite for two reasons.

The first is that a customer will ask. "How good is it" is the second question
in every defence and infrastructure procurement, and the answer has to be a
number produced by a repeatable procedure, not a claim.

The second is that the numbers move. Thresholds here were tuned against the
synthetic estate in sites.py, and a customer's imagery -- different sensors,
different terrain, different object mix -- will need them retuned. `score_scene`
against a few hand-labelled scenes is how that is done, and it is the same
call the tests make.

Matching is by distance and class, with a deliberately generous radius: at 10 m
a detection three cells off the truth centroid is a hit, not a miss, because an
analyst clicking the alert lands on the right object either way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .domain import Detection, ObjectClass
from .geo import LocalFrame, frame_for
from .world import Feature, SiteTruth


@dataclass
class Score:
    """Precision, recall and the counts they came from."""

    matched: int = 0
    missed: int = 0
    spurious: int = 0
    by_class: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    confidence_pairs: list[tuple[float, bool]] = field(default_factory=list)
    #: Scene-km2 scored. Precision is a poor summary over near-empty terrain --
    #: one false positive against zero true objects reads as precision 0,
    #: whatever the area -- so false positives per square kilometre per look is
    #: the number to quote for a border sector, and the one a customer can
    #: convert into analyst-hours.
    area_km2: float = 0.0

    @property
    def precision(self) -> float:
        d = self.matched + self.spurious
        return self.matched / d if d else 1.0

    @property
    def recall(self) -> float:
        d = self.matched + self.missed
        return self.matched / d if d else 1.0

    @property
    def false_positives_per_km2(self) -> float:
        return self.spurious / self.area_km2 if self.area_km2 else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    def to_dict(self) -> dict:
        return {
            "matched": self.matched, "missed": self.missed,
            "spurious": self.spurious,
            "precision": round(self.precision, 3),
            "recall": round(self.recall, 3),
            "f1": round(self.f1, 3),
            "area_km2": round(self.area_km2, 1),
            "false_positives_per_km2": round(self.false_positives_per_km2, 3),
            "by_class": {k: {"matched": v[0], "missed": v[1], "spurious": v[2]}
                         for k, v in sorted(self.by_class.items())},
        }

    def add(self, other: "Score") -> None:
        self.matched += other.matched
        self.missed += other.missed
        self.spurious += other.spurious
        self.area_km2 += other.area_km2
        self.confidence_pairs.extend(other.confidence_pairs)
        for k, (m, mi, s) in other.by_class.items():
            a, b, c = self.by_class.get(k, (0, 0, 0))
            self.by_class[k] = (a + m, b + mi, c + s)


#: Classes an analyst would accept as the same call. A container stack reported
#: as a truck at 10 m is a labelling quibble; a building reported as a vessel is
#: not. Scoring that ignores this understates the engine, and scoring that
#: ignores the second case overstates it.
EQUIVALENT: tuple[frozenset[ObjectClass], ...] = (
    frozenset({ObjectClass.TRUCK, ObjectClass.CONTAINER_STACK,
               ObjectClass.CONSTRUCTION_VEHICLE}),
    frozenset({ObjectClass.VEHICLE, ObjectClass.CONSTRUCTION_VEHICLE}),
    frozenset({ObjectClass.VESSEL, ObjectClass.SMALL_BOAT}),
    frozenset({ObjectClass.BUILDING, ObjectClass.STORAGE_TANK}),
    frozenset({ObjectClass.ROAD, ObjectClass.RUNWAY, ObjectClass.BRIDGE,
               ObjectClass.TOWER}),
)


def compatible(a: ObjectClass, b: ObjectClass) -> bool:
    if a is b:
        return True
    return any(a in group and b in group for group in EQUIVALENT)


def score_scene(truth: SiteTruth, boundary, when: date,
                detections: list[Detection],
                classes: set[ObjectClass] | None = None,
                radius_factor: float = 0.75,
                min_extent_m: float = 0.0,
                min_width_m: float = 0.0,
                area_km2: float = 0.0) -> Score:
    """Match detections against what was actually on the ground that day.

    `min_extent_m` and `min_width_m` exclude truth objects the sensor could not
    have resolved, so recall is measured against what was recoverable rather
    than against what existed. Scoring a 10 m scene for its failure to find
    cars produces a recall figure that says nothing about the detector.

    Both dimensions matter, not just the longer one. A 4 km road 12 m wide is
    long enough to pass an extent test and still only one pixel across in
    Sentinel-2, where segmenting it needs a directional filter bank this engine
    does not have. Counting it as a miss would blame the detector for a real and
    separately tracked gap; counting it as out of scope states the gap instead.
    """
    frame: LocalFrame = frame_for(boundary)
    present = [f for f in truth.features_on(when) + truth.transients_on(when)
               if classes is None or f.object_class in classes]

    def resolvable(f: Feature) -> bool:
        return (max(f.length_m, f.width_m) >= min_extent_m
                and min(f.length_m, f.width_m) >= min_width_m)

    expected = [f for f in present if resolvable(f)]
    #: Objects that are really there but below what the sensor can resolve.
    #: A detection that lands on one of these is not a false positive -- the
    #: detector was right, it simply found something the scoring rules do not
    #: hold it responsible for. Counting it against precision would punish the
    #: engine for being better than its own success criteria, and would make
    #: the port look worse than the desert purely because the port is busier.
    ignored = [f for f in present if not resolvable(f)]
    pool = [d for d in detections if classes is None or d.object_class in classes]

    score = Score(area_km2=area_km2)
    taken: set[str] = set()

    def nearest(f: Feature) -> tuple[float, Detection] | None:
        best: tuple[float, Detection] | None = None
        for d in pool:
            if d.id in taken or not compatible(d.object_class, f.object_class):
                continue
            de, dn = frame.to_m((d.lon, d.lat))
            dist = ((de - f.east_m) ** 2 + (dn - f.north_m) ** 2) ** 0.5
            reach = radius_factor * max(f.length_m, f.width_m, d.extent_m)
            if dist <= reach and (best is None or dist < best[0]):
                best = (dist, d)
        return best

    for f in expected:
        best = nearest(f)
        key = f.object_class.value
        m, mi, s_ = score.by_class.get(key, (0, 0, 0))
        if best is not None:
            taken.add(best[1].id)
            score.matched += 1
            score.by_class[key] = (m + 1, mi, s_)
            score.confidence_pairs.append((best[1].confidence, True))
        else:
            score.missed += 1
            score.by_class[key] = (m, mi + 1, s_)

    for f in ignored:
        best = nearest(f)
        if best is not None:
            taken.add(best[1].id)

    for d in pool:
        if d.id not in taken:
            score.spurious += 1
            key = d.object_class.value
            m, mi, s = score.by_class.get(key, (0, 0, 0))
            score.by_class[key] = (m, mi, s + 1)
            score.confidence_pairs.append((d.confidence, False))
    return score
