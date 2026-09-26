"""Fusion: turning many detections into the few events an analyst should read.

A monitoring pipeline produces one change record per comparison. A construction
site seen on eleven passes therefore produces eleven records, and an alert queue
built straight from them shows the same excavation eleven times. That is the
mechanism behind alert fatigue, and it is not solved by raising a threshold --
raising it loses the small real things and keeps the repeated large ones.

What solves it is recognising that those eleven records are *one event with a
duration*. This module does that grouping: same place, compatible kind, and a
gap no longer than the sensor's own revisit. What comes out has a beginning, a
most recent look, a count of how many times it was seen, and a trajectory.

Three properties that matter downstream:

**Persistence becomes real evidence rather than a count.** Something seen once
could be a cloud edge, a shadow or a registration artefact. Something seen on
six consecutive passes at the same footprint is a fact about the ground. The
event carries `looks`, and `risk.py` already prefers findings confirmed by a
second look.

**Novelty is measured against the site's own record, not globally.** A new
building at a construction site is routine; the same building appearing in a
salt flat is not. Novelty is computed per AOI per kind.

**An event is never a conclusion about intent.** `EventKind` names what the
imagery shows -- construction started, a surface was disturbed, water extent
moved. It does not name a purpose, and `headline()` is tested against the
vocabulary of threat, the same way `anomaly.py` is.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum

from .domain import ChangeEvent, ChangeType, Severity
from .geo import haversine_m

# ---------------------------------------------------------------------------
# What an event is
# ---------------------------------------------------------------------------


class EventKind(str, Enum):
    """What the imagery shows, never why.

    Deliberately close to the change taxonomy rather than a layer of
    interpretation on top of it. Every step away from the measurement is a step
    the analyst has to audit.
    """

    CONSTRUCTION = "construction"            # structures appearing or growing
    DEMOLITION = "demolition"                # structures removed
    GROUND_DISTURBANCE = "ground_disturbance"  # earthworks, materials, clearing
    ACCESS_DEVELOPMENT = "access_development"  # tracks, roads, berms
    WATER_EXTENT = "water_extent"            # inundation or drawdown
    SURFACE_CHANGE = "surface_change"        # resurfacing, stockpiles, cover
    POSSIBLE_DAMAGE = "possible_damage"      # structure altered destructively
    OBJECT_PRESENCE = "object_presence"      # things that come and go


#: Which change types compose which events. A change type absent here does not
#: form events -- object arrivals and departures are movement, already handled
#: as population counts, and filing each as an event recreates the noise this
#: module exists to remove.
COMPOSES: dict[ChangeType, EventKind] = {
    ChangeType.NEW_STRUCTURE: EventKind.CONSTRUCTION,
    ChangeType.STRUCTURE_REMOVED: EventKind.DEMOLITION,
    ChangeType.CONSTRUCTION_ACTIVITY: EventKind.GROUND_DISTURBANCE,
    ChangeType.LINEAR_FEATURE: EventKind.ACCESS_DEVELOPMENT,
    ChangeType.INUNDATION: EventKind.WATER_EXTENT,
    ChangeType.SURFACE_CHANGE: EventKind.SURFACE_CHANGE,
    ChangeType.POSSIBLE_DAMAGE: EventKind.POSSIBLE_DAMAGE,
}

#: Kinds that can merge into one another as a site progresses. Ground gets
#: disturbed, then a structure appears, and that is one construction sequence
#: rather than two unrelated events at the same coordinates.
SEQUENCES: dict[EventKind, set[EventKind]] = {
    EventKind.CONSTRUCTION: {EventKind.GROUND_DISTURBANCE,
                             EventKind.ACCESS_DEVELOPMENT},
    EventKind.GROUND_DISTURBANCE: {EventKind.CONSTRUCTION,
                                   EventKind.ACCESS_DEVELOPMENT},
    EventKind.ACCESS_DEVELOPMENT: {EventKind.GROUND_DISTURBANCE,
                                   EventKind.CONSTRUCTION},
}

#: How close two findings must be to be the same event. Generous, because a
#: change mask's centroid moves as the footprint grows: a building under
#: construction is measured from its excavation on one pass and its full slab
#: on the next, and those centroids can sit a hundred metres apart while
#: describing one thing.
SAME_PLACE_M = 250.0

#: The longest gap that still counts as the same episode. Twelve days is two
#: Sentinel-2 revisits: a site quiet for longer than two chances to see it has
#: stopped, and resuming later is a new episode rather than a continuation.
CONTINUITY_DAYS = 12

#: Below this many looks, persistence is not yet evidence of anything. Two is
#: the minimum that distinguishes a repeated observation from a single one.
CONFIRMING_LOOKS = 2


@dataclass
class Event:
    """One thing that happened, seen possibly many times."""

    id: str
    aoi_id: str
    kind: EventKind
    lon: float
    lat: float
    started_on: date
    last_seen_on: date
    findings: list[ChangeEvent] = field(default_factory=list)
    #: Kinds that merged in, so a construction sequence can say it began as
    #: ground disturbance rather than silently relabelling its own history.
    composed_of: list[EventKind] = field(default_factory=list)

    # -- measures ----------------------------------------------------------

    @property
    def looks(self) -> int:
        return len(self.findings)

    @property
    def duration_days(self) -> int:
        return (self.last_seen_on - self.started_on).days

    @property
    def peak_area_m2(self) -> float:
        return max((f.area_m2 for f in self.findings), default=0.0)

    @property
    def latest_area_m2(self) -> float:
        return self.findings[-1].area_m2 if self.findings else 0.0

    @property
    def severity(self) -> Severity:
        """The worst any single look reported.

        Not an average. An event that looked critical once and medium five
        times is an event that looked critical, and averaging it away is how a
        real finding gets buried by its own follow-ups.
        """
        worst = Severity.LOW
        for f in self.findings:
            if f.severity.rank > worst.rank:
                worst = f.severity
        return worst

    @property
    def confidence(self) -> float:
        """Best single-look confidence, raised a little by repetition.

        Seeing the same thing again is genuine corroboration, but it is weak
        corroboration -- a systematic error repeats too. Capped so repetition
        alone can never manufacture certainty.
        """
        best = max((f.confidence for f in self.findings), default=0.0)
        if self.looks < CONFIRMING_LOOKS:
            return best
        bonus = min(0.10, 0.02 * (self.looks - 1))
        return min(0.99, best + bonus)

    @property
    def confirmed(self) -> bool:
        """Seen enough times that a transient artefact is ruled out."""
        return self.looks >= CONFIRMING_LOOKS

    @property
    def trajectory(self) -> str:
        """Growing, shrinking or steady, by area across looks."""
        if self.looks < 2:
            return "single look"
        first, last = self.findings[0].area_m2, self.findings[-1].area_m2
        if first <= 0:
            return "steady"
        ratio = last / first
        if ratio >= 1.25:
            return "growing"
        if ratio <= 0.8:
            return "shrinking"
        return "steady"

    def headline(self) -> str:
        """What happened, where and over how long. Never why."""
        noun = {
            EventKind.CONSTRUCTION: "structures appearing",
            EventKind.DEMOLITION: "structures removed",
            EventKind.GROUND_DISTURBANCE: "ground disturbed",
            EventKind.ACCESS_DEVELOPMENT: "access route extended",
            EventKind.WATER_EXTENT: "water extent moved",
            EventKind.SURFACE_CHANGE: "surface changed",
            EventKind.POSSIBLE_DAMAGE: "structure altered",
            EventKind.OBJECT_PRESENCE: "objects present",
        }[self.kind]

        if self.looks == 1:
            seen = (f"seen once, on {self.started_on.isoformat()}, so a "
                    f"transient artefact is not yet ruled out")
        else:
            seen = (f"seen {self.looks} times over {self.duration_days} days "
                    f"from {self.started_on.isoformat()}, {self.trajectory}")
        return (f"{noun} at {self.aoi_id}: {self.latest_area_m2:,.0f} m2, "
                f"{seen}")

    def to_dict(self) -> dict:
        return {
            "id": self.id, "aoi_id": self.aoi_id, "kind": self.kind.value,
            "lon": round(self.lon, 6), "lat": round(self.lat, 6),
            "started_on": self.started_on.isoformat(),
            "last_seen_on": self.last_seen_on.isoformat(),
            "duration_days": self.duration_days,
            "looks": self.looks, "confirmed": self.confirmed,
            "trajectory": self.trajectory,
            "severity": self.severity.value,
            "confidence": round(self.confidence, 3),
            "peak_area_m2": round(self.peak_area_m2, 1),
            "latest_area_m2": round(self.latest_area_m2, 1),
            "composed_of": [k.value for k in self.composed_of],
            "finding_ids": [f.id for f in self.findings],
            "evidence_ids": [f.evidence_id for f in self.findings
                             if f.evidence_id],
            "headline": self.headline(),
        }


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------

def _centroid(change: ChangeEvent) -> tuple[float, float] | None:
    """The finding's location, from whatever geometry it carries."""
    geom = change.geometry
    if not geom:
        return None
    ring = geom.get("coordinates") if isinstance(geom, dict) else None
    if isinstance(ring, list) and ring and isinstance(ring[0], list):
        pts = ring[0] if isinstance(ring[0][0], (list, tuple)) else ring
        try:
            xs = [float(p[0]) for p in pts]
            ys = [float(p[1]) for p in pts]
        except (TypeError, ValueError, IndexError):
            return None
        if xs and ys:
            return sum(xs) / len(xs), sum(ys) / len(ys)
    return None


def _event_id(aoi_id: str, kind: EventKind, lon: float, lat: float,
              started: date) -> str:
    """Stable across re-runs, so re-fusing the same history does not produce a
    new set of event ids and a fresh wave of alerts."""
    seed = f"{aoi_id}|{kind.value}|{lon:.4f}|{lat:.4f}|{started.isoformat()}"
    return "evt-" + hashlib.sha256(seed.encode()).hexdigest()[:12]


def _joins(event: Event, kind: EventKind, lon: float, lat: float,
           when: date) -> bool:
    """Is this finding another look at that event?"""
    if kind is not event.kind and kind not in SEQUENCES.get(event.kind, set()):
        return False
    if when < event.last_seen_on:
        return False
    if (when - event.last_seen_on).days > CONTINUITY_DAYS:
        return False
    return haversine_m((lon, lat), (event.lon, event.lat)) <= SAME_PLACE_M


def fuse(changes: list[ChangeEvent]) -> list[Event]:
    """Group change findings into events.

    Chronological single pass: each finding either extends an open event or
    starts a new one. Order matters -- an event's identity is its first
    sighting, so replaying the same history always produces the same events.
    """
    ordered = sorted(
        (c for c in changes if c.change_type in COMPOSES),
        key=lambda c: (c.detected_at, c.id))

    events: list[Event] = []
    for change in ordered:
        kind = COMPOSES[change.change_type]
        centre = _centroid(change)
        if centre is None:
            #: No geometry means no way to tell whether it is the same place as
            #: anything else. It becomes its own event rather than being
            #: dropped -- a finding the analyst cannot see is worse than one
            #: that failed to merge.
            centre = (0.0, 0.0)
        lon, lat = centre
        when = change.detected_at.date() if hasattr(change.detected_at, "date") \
            else change.detected_at

        for event in reversed(events):
            if event.aoi_id != change.aoi_id:
                continue
            if _joins(event, kind, lon, lat, when):
                event.findings.append(change)
                event.last_seen_on = when
                if kind is not event.kind and kind not in event.composed_of:
                    event.composed_of.append(kind)
                #: A sequence that reaches construction is a construction
                #: event: the most developed stage names the whole episode.
                if (kind is EventKind.CONSTRUCTION
                        and event.kind is not EventKind.CONSTRUCTION):
                    if event.kind not in event.composed_of:
                        event.composed_of.append(event.kind)
                    event.kind = EventKind.CONSTRUCTION
                break
        else:
            events.append(Event(
                id=_event_id(change.aoi_id, kind, lon, lat, when),
                aoi_id=change.aoi_id, kind=kind, lon=lon, lat=lat,
                started_on=when, last_seen_on=when, findings=[change]))
    return events


def novelty(event: Event, history: list[Event]) -> float:
    """How unusual this kind of event is at this site, from 0 to 1.

    Measured against the site's own record. A new structure at a construction
    site is routine; the same structure on a salt flat is not, and a global
    novelty score cannot tell the two apart.
    """
    prior = [e for e in history
             if e.aoi_id == event.aoi_id and e.id != event.id
             and e.started_on < event.started_on]
    if not prior:
        return 1.0
    same_kind = sum(1 for e in prior if e.kind is event.kind)
    return max(0.0, 1.0 - (same_kind / len(prior)))


def summarise(events: list[Event]) -> dict:
    """Counts for the events feed, and the number that matters for fatigue."""
    findings = sum(e.looks for e in events)
    by_kind: dict[str, int] = {}
    for e in events:
        by_kind[e.kind.value] = by_kind.get(e.kind.value, 0) + 1
    return {
        "events": len(events),
        "findings": findings,
        #: What the queue would have shown without fusion, against what it
        #: shows with it. This is the alert-noise number the scope of work asks
        #: to be reported per milestone.
        "reduction": (round(1.0 - len(events) / findings, 3)
                      if findings else 0.0),
        "confirmed": sum(1 for e in events if e.confirmed),
        "single_look": sum(1 for e in events if not e.confirmed),
        "by_kind": by_kind,
    }
