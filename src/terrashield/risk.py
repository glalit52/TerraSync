"""Risk scoring: the five dimensions an analyst actually triages on.

PRD section 18 lists severity, confidence, novelty, persistence and spatial
significance, and keeping them as five numbers rather than collapsing them into
one is the whole point. They answer different questions and they disagree
usefully:

  severity      how much would this matter if it is real
  confidence    how sure are we that it is real
  novelty       has this site ever looked like this before
  persistence   is it still there, or was it a one-look artefact
  spatial       how much of the AOI it affects

A single blended number cannot distinguish a confident observation of something
minor from a shaky observation of something major, and those need opposite
responses. The composite exists only to order a queue; the five components are
what is shown beside it, and what the analyst argues with.

Persistence deserves particular note because it resolves the ambiguity the
change engine cannot. At 10 m, a cluster of shipping containers arriving in a
port yard and a new building going up produce nearly identical evidence in a
single pair. What separates them is that the containers are gone next week.
"""

from __future__ import annotations

from dataclasses import dataclass

from .domain import Aoi, ChangeEvent, ChangeType, Severity


@dataclass
class RiskScore:
    """Five dimensions and an ordering key, never a statement of intent."""

    severity: Severity
    confidence: float           # 0..1, from the engine that produced the finding
    novelty: float              # 0..1, distance from anything in the site's record
    persistence: float          # 0..1, share of subsequent looks that still show it
    spatial: float              # 0..1, share of the AOI affected
    looks: int = 1              # how many comparisons this has been seen in

    @property
    def composite(self) -> float:
        """0..100, for ranking only.

        Severity sets the ceiling and confidence scales it, because an
        unconfirmed observation of something serious should not outrank a
        confirmed one of the same thing. Novelty, persistence and spatial reach
        adjust within that band rather than across it.
        """
        base = {Severity.LOW: 25.0, Severity.MEDIUM: 50.0,
                Severity.HIGH: 75.0, Severity.CRITICAL: 92.0}[self.severity]
        modifier = (0.45 * self.novelty + 0.35 * self.persistence
                    + 0.20 * self.spatial)
        return round(min(100.0, base * (0.55 + 0.45 * self.confidence)
                         * (0.80 + 0.40 * modifier)), 1)

    def to_dict(self) -> dict:
        return {
            "severity": self.severity.value,
            "confidence": round(self.confidence, 3),
            "novelty": round(self.novelty, 3),
            "persistence": round(self.persistence, 3),
            #: Five places, not three. The other four dimensions span 0..1
            #: and three is plenty; spatial is a share of an entire monitored
            #: area, so a building-sized footprint is legitimately around one
            #: part in ten thousand. Rounded to three it becomes exactly zero
            #: and the console prints "0.00%", which reads as "none" for a
            #: change that is small but certainly there.
            "spatial": round(self.spatial, 5),
            "looks": self.looks,
            "composite": self.composite,
        }

    def explain(self) -> list[str]:
        out = [
            f"severity {self.severity.value}: how much this would matter if confirmed",
            f"confidence {self.confidence:.0%}: how well the imagery supports it",
        ]
        if self.novelty >= 0.7:
            out.append(f"novelty {self.novelty:.0%}: nothing comparable in this "
                       "site's recorded history")
        elif self.novelty <= 0.3:
            out.append(f"novelty {self.novelty:.0%}: this site has looked like "
                       "this before")
        if self.looks > 1:
            others = self.looks - 1
            out.append(f"persistence {self.persistence:.0%}: also flagged in "
                       f"{round(self.persistence * others)} of {others} other "
                       "comparisons covering this place")
        else:
            out.append("persistence not yet established: this is the only "
                       "comparison covering this place, so a transient object "
                       "has not been ruled out")
        #: A footprint that is a ten-thousandth of an AOI is still a real
        #: footprint, so the wording degrades rather than rounding to "0.0%".
        if self.spatial >= 0.001:
            out.append(f"spatial reach {self.spatial:.1%} of the monitored area")
        elif self.spatial > 0:
            out.append("spatial reach under a tenth of a percent of the "
                       "monitored area")
        else:
            out.append("spatial reach too small to express as a share of the "
                       "monitored area")
        return out


#: Area above which one look is decisive enough to escalate a novel, serious
#: finding without waiting for confirmation.
#:
#: Novelty alone is not enough, and relying on it was a mistake worth naming.
#: On a near-empty site every speckle artefact is novel -- there is nothing on
#: record for it to resemble -- so "novel and high severity" exempted a stream
#: of 1,800 m2 blobs on a salt flat from the hold and put fifty of them in the
#: queue at priority 2 over six months. Twenty times the minimum detectable
#: change is a structure nobody argues about; below that, wait for the next
#: pass, which is exactly what persistence now provides.
DECISIVE_AREA_M2 = 18_000.0

#: Change types that a single look genuinely cannot separate from an object
#: simply being parked there. Their persistence is unknown until a later look,
#: and this is the set the queue holds back rather than escalating.
AMBIGUOUS_ON_ONE_LOOK = frozenset({
    ChangeType.NEW_STRUCTURE, ChangeType.CONSTRUCTION_ACTIVITY,
    ChangeType.SURFACE_CHANGE,
})


def novelty_of(event: ChangeEvent, history: list[ChangeEvent]) -> float:
    """How unlike anything in the site's record this change is.

    Compared on type and size, not location: a port that has seen three
    warehouses go up has seen a warehouse go up, wherever the fourth one lands.
    """
    same_type = [h for h in history
                 if h.change_type is event.change_type and h.id != event.id]
    if not same_type:
        return 1.0
    ratios = [max(event.area_m2, h.area_m2) / max(min(event.area_m2, h.area_m2), 1.0)
              for h in same_type]
    closest = min(ratios)
    #: Within a factor of two of something already seen is not novel; an order
    #: of magnitude larger than anything on record is.
    return round(min(1.0, max(0.0, (closest - 1.5) / 6.5)), 3)


def persistence_of(event: ChangeEvent, others: list[ChangeEvent],
                   overlap_m: float = 150.0) -> tuple[float, int]:
    """Share of the other comparisons at this AOI that also flagged this place.

    Direction-agnostic on purpose. Measuring persistence by looking *forward*
    reads well and cannot be done by a pipeline running a day at a time, which
    only has the past: pass it the future and it is always empty, so every
    finding is permanently "seen once" and nothing is ever confirmed. Looking
    backward answers the same question -- has this place been flagged before,
    or is this the first time -- from evidence that exists.

    `others` should be the comparisons made at the same AOI over a recent
    window, not a global history: the denominator is how many chances there
    were to see it again.
    """
    from .geo import haversine_m
    if not others:
        return 0.0, 1
    by_look: dict[str, bool] = {}
    for h in others:
        if h.id == event.id:
            continue
        seen = by_look.get(h.after_scene_id, False)
        if haversine_m(event.centroid, h.centroid) <= overlap_m:
            seen = True
        by_look[h.after_scene_id] = seen
    by_look.pop(event.after_scene_id, None)   # the comparison this came from
    if not by_look:
        return 0.0, 1
    return (round(sum(1 for v in by_look.values() if v) / len(by_look), 3),
            len(by_look) + 1)


def score_change(event: ChangeEvent, aoi: Aoi,
                 history: list[ChangeEvent] | None = None,
                 others: list[ChangeEvent] | None = None) -> RiskScore:
    """Score one finding.

    `history` is what this site has looked like before, and answers novelty.
    `others` is the set of comparisons that had a chance to see this same
    place, and answers persistence. They are usually the same list.
    """
    history = history or []
    persistence, looks = persistence_of(event, others if others is not None
                                        else history)
    spatial = min(1.0, event.area_m2 / max(aoi.area_km2 * 1_000_000, 1.0))
    return RiskScore(
        severity=event.severity,
        confidence=event.confidence,
        novelty=novelty_of(event, history),
        persistence=persistence,
        spatial=round(spatial, 5),
        looks=looks,
    )


def held_for_confirmation(event: ChangeEvent, score: RiskScore) -> str:
    """Why this finding is not being escalated yet, or an empty string.

    A single look at a port cannot tell a stack of containers from a building.
    Saying so and waiting for the next pass is better than either guessing or
    staying silent -- the finding stays in the queue, marked.

    What releases the hold is being *seen again*, not merely the existence of
    other looks. Those are opposite pieces of evidence and conflating them
    inverts the logic: a change flagged once across four comparisons that all
    covered it is better evidence of something moveable than a change flagged
    once with nothing to compare against.
    """
    if event.change_type not in AMBIGUOUS_ON_ONE_LOOK:
        return ""
    if score.persistence > 0:
        return ""       # another comparison saw it too: not a one-look artefact
    if (event.severity.rank >= Severity.HIGH.rank and score.novelty >= 0.8
            and event.area_m2 >= DECISIVE_AREA_M2):
        return ""       # novel, serious and large enough to act on one look
    if score.looks > 1:
        others = score.looks - 1
        return (f"flagged once, and {others} other comparison(s) covering this "
                "place did not show it. Consistent with something moveable; "
                "held pending a look that confirms it")
    return ("seen in one comparison only; a moveable object has not been ruled "
            "out. Held for confirmation against the next usable scene")
