"""Rules, alerts, and the queue an analyst actually works.

PRD section 19 asks for user-defined rules and section 20 asks for ranking
instead of volume. Section 20 is the hard one and the one the product lives or
dies on: a monitoring platform that emits everything it notices is a platform
that gets muted in week two, and a muted platform detects nothing at all,
however good its models are.

Four mechanisms keep the queue workable, and none of them is a confidence
threshold:

**Rules are declarative and auditable.** A rule is data -- conditions, an
action, an owner -- not a lambda. It can be stored, shown in the interface,
diffed, and explained in the alert it produced. "Why did I get this" is
answerable by quoting the rule that matched.

**Deduplication is by place and kind, not by identity.** The same construction
site produces a change event every pass for two months. That is one alert with
a rising occurrence count, not forty.

**Suppression windows are per rule and per AOI.** A site under active watch
should not re-alert daily for the thing the analyst already has open.

**Priority is a queue position, not a severity label.** Priority 1 means work
this first, and the ranking uses the composite risk score, which already
accounts for whether a finding has been confirmed by a second look.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any

from .domain import (
    Alert, AnomalyFinding, Aoi, ChangeEvent, ChangeType, Severity,
)
from .risk import RiskScore, held_for_confirmation
from .world import seed_of


class Op(str, Enum):
    """Comparison operators a rule condition can use."""

    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    CONTAINS = "contains"


def _compare(left: Any, op: Op, right: Any) -> bool:
    try:
        if op is Op.EQ:
            return left == right
        if op is Op.NE:
            return left != right
        if op is Op.GT:
            return left > right
        if op is Op.GTE:
            return left >= right
        if op is Op.LT:
            return left < right
        if op is Op.LTE:
            return left <= right
        if op is Op.IN:
            return left in right
        if op is Op.CONTAINS:
            return right in left
    except TypeError:
        return False
    return False


@dataclass(frozen=True)
class Condition:
    """One `field op value` test against a finding's flattened attributes."""

    field: str
    op: Op
    value: Any

    def holds(self, facts: dict) -> bool:
        if self.field not in facts:
            return False
        return _compare(facts[self.field], self.op, self.value)

    def describe(self) -> str:
        words = {
            Op.EQ: "is", Op.NE: "is not", Op.GT: "is above",
            Op.GTE: "is at least", Op.LT: "is below", Op.LTE: "is at most",
            Op.IN: "is one of", Op.CONTAINS: "contains",
        }[self.op]
        return f"{self.field.replace('_', ' ')} {words} {self.value}"


@dataclass
class Rule:
    """A named, storable condition set. All conditions must hold."""

    id: str
    org_id: str
    name: str
    conditions: list[Condition]
    severity_floor: Severity = Severity.LOW
    aoi_ids: list[str] = field(default_factory=list)   # empty means every AOI
    aoi_kinds: list[str] = field(default_factory=list)
    channels: list[str] = field(default_factory=lambda: ["dashboard"])
    #: Do not raise this rule again for the same AOI and finding kind inside
    #: this many days. The single most effective anti-fatigue control there is.
    suppress_days: int = 7
    enabled: bool = True
    created_by: str = ""

    def applies_to(self, aoi: Aoi) -> bool:
        if self.aoi_ids and aoi.id not in self.aoi_ids:
            return False
        if self.aoi_kinds and aoi.kind.value not in self.aoi_kinds:
            return False
        return self.enabled

    def matches(self, facts: dict) -> bool:
        return all(c.holds(facts) for c in self.conditions)

    def describe(self) -> str:
        joined = " AND ".join(c.describe() for c in self.conditions)
        scope = ", ".join(self.aoi_ids) if self.aoi_ids else (
            ", ".join(self.aoi_kinds) if self.aoi_kinds else "every monitored area")
        return f"IF {joined} (scope: {scope}) THEN alert via " \
               f"{', '.join(self.channels)}"

    def to_dict(self) -> dict:
        return {
            "id": self.id, "org_id": self.org_id, "name": self.name,
            "enabled": self.enabled,
            "conditions": [{"field": c.field, "op": c.op.value, "value": c.value}
                           for c in self.conditions],
            "severity_floor": self.severity_floor.value,
            "aoi_ids": list(self.aoi_ids), "aoi_kinds": list(self.aoi_kinds),
            "channels": list(self.channels), "suppress_days": self.suppress_days,
            "created_by": self.created_by, "description": self.describe(),
        }


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------

def change_facts(event: ChangeEvent, aoi: Aoi, score: RiskScore) -> dict:
    """Flatten a change event into the names rules are written against."""
    return {
        "finding_kind": "change",
        "change_type": event.change_type.value,
        "severity": event.severity.value,
        "severity_rank": event.severity.rank,
        "confidence": event.confidence,
        "area_m2": event.area_m2,
        "magnitude": event.magnitude,
        "aoi_id": aoi.id,
        "aoi_kind": aoi.kind.value,
        "novelty": score.novelty,
        "persistence": score.persistence,
        "looks": score.looks,
        "spatial_fraction": score.spatial,
        "risk": score.composite,
    }


def anomaly_facts(finding: AnomalyFinding, aoi: Aoi) -> dict:
    return {
        "finding_kind": "anomaly",
        "anomaly_score": finding.score,
        "severity": finding.severity.value,
        "severity_rank": finding.severity.rank,
        "confidence": finding.confidence,
        "aoi_id": aoi.id,
        "aoi_kind": aoi.kind.value,
        "baseline_n": finding.baseline_n,
        "drivers": sorted(finding.contributions),
        "risk": finding.score,
    }


# ---------------------------------------------------------------------------
# Default rules
# ---------------------------------------------------------------------------

def default_rules(org_id: str) -> list[Rule]:
    """A working starter set. Every one of these is a sentence from the PRD.

    Deliberately few. A platform that ships forty rules enabled produces forty
    rules' worth of noise on day one, and the customer's first experience of it
    is triage. Five rules that fire rarely and correctly earn the right to add
    a sixth.
    """
    return [
        Rule(id="rule-new-structure", org_id=org_id,
             name="New structure at a monitored site",
             conditions=[
                 Condition("finding_kind", Op.EQ, "change"),
                 Condition("change_type", Op.IN,
                           [ChangeType.NEW_STRUCTURE.value,
                            ChangeType.STRUCTURE_REMOVED.value]),
                 Condition("confidence", Op.GTE, 0.6),
                 Condition("area_m2", Op.GTE, 1500),
             ],
             severity_floor=Severity.MEDIUM,
             channels=["dashboard", "email"]),
        Rule(id="rule-border-activity", org_id=org_id,
             name="Construction or new track in a remote sector",
             conditions=[
                 Condition("finding_kind", Op.EQ, "change"),
                 Condition("change_type", Op.IN,
                           [ChangeType.CONSTRUCTION_ACTIVITY.value,
                            ChangeType.LINEAR_FEATURE.value,
                            ChangeType.NEW_STRUCTURE.value]),
                 Condition("confidence", Op.GTE, 0.55),
             ],
             aoi_kinds=["border_sector"],
             severity_floor=Severity.LOW,
             channels=["dashboard", "webhook"], suppress_days=3),
        Rule(id="rule-infrastructure-damage", org_id=org_id,
             name="Possible damage to critical infrastructure",
             conditions=[
                 Condition("finding_kind", Op.EQ, "change"),
                 Condition("change_type", Op.EQ, ChangeType.POSSIBLE_DAMAGE.value),
             ],
             aoi_kinds=["energy", "dam", "port", "transport", "industrial"],
             severity_floor=Severity.HIGH,
             channels=["dashboard", "email", "webhook"], suppress_days=1),
        Rule(id="rule-inundation", org_id=org_id,
             name="Water extent change at a dam or in a flood-prone area",
             conditions=[
                 Condition("finding_kind", Op.EQ, "change"),
                 Condition("change_type", Op.IN,
                           [ChangeType.INUNDATION.value,
                            ChangeType.SURFACE_CHANGE.value]),
                 Condition("area_m2", Op.GTE, 250_000),
             ],
             aoi_kinds=["dam"], severity_floor=Severity.MEDIUM,
             channels=["dashboard", "email"]),
        Rule(id="rule-activity-deviation", org_id=org_id,
             name="Activity well outside the site's historical envelope",
             conditions=[
                 Condition("finding_kind", Op.EQ, "anomaly"),
                 Condition("anomaly_score", Op.GTE, 65),
                 Condition("confidence", Op.GTE, 0.5),
                 Condition("baseline_n", Op.GTE, 12),
             ],
             severity_floor=Severity.MEDIUM,
             channels=["dashboard"], suppress_days=5),
    ]


# ---------------------------------------------------------------------------
# Raising
# ---------------------------------------------------------------------------

@dataclass
class RaisedAlert:
    """An alert plus why it exists, kept together so the interface can show both."""

    alert: Alert
    #: The rule that best explains the alert -- the narrowest matching scope.
    rule: Rule
    #: Every rule that matched this finding. An analyst wants one row per
    #: thing that happened, not one per rule that noticed it: a new structure
    #: in a border sector matches both the general structure rule and the
    #: border rule, and raising two identical alerts for it doubles the queue
    #: while adding nothing.
    rules: list[Rule] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    #: The key this alert was deduplicated under. Carried on the object rather
    #: than recomputed by the caller: the pipeline used to re-derive it by
    #: searching for any key starting with the rule id, which returned an
    #: arbitrary one whenever a rule matched more than one finding. The stored
    #: key then did not describe the alert, so the suppression window never
    #: matched on the next run and a single construction site re-alerted on
    #: every pass for two months.
    dedup_key: str = ""
    risk: RiskScore | None = None
    held_reason: str = ""
    occurrences: int = 1

    def to_dict(self) -> dict:
        a = self.alert
        return {
            "id": a.id, "aoi_id": a.aoi_id, "rule_id": a.rule_id,
            "rule_name": self.rule.name, "title": a.title,
            "dedup_key": self.dedup_key,
            "severity": a.severity.value, "priority": a.priority,
            "created_at": a.created_at.isoformat(), "summary": a.summary,
            "change_event_ids": list(a.change_event_ids),
            "anomaly_ids": list(a.anomaly_ids),
            "matched_conditions": list(self.matched),
            "rule": self.rule.describe(),
            "matched_rules": [r.name for r in self.rules] or [self.rule.name],
            "risk": self.risk.to_dict() if self.risk else None,
            "held_for_confirmation": self.held_reason,
            "occurrences": self.occurrences,
            "review_status": a.review_status.value,
            "evidence_id": a.evidence_id,
            "channels": list(self.rule.channels),
        }


def _dedup_key(aoi_id: str, facts: dict) -> str:
    """Same AOI, same kind of thing, roughly the same place.

    Deliberately not keyed by rule. Keyed by rule, a new structure in a border
    sector matches both the general structure rule and the border rule and
    produces two identical alerts, and the queue grows with the rule set rather
    than with what is happening on the ground.

    Location is bucketed to a coarse grid rather than compared exactly, because
    a construction site's change polygon wanders by tens of metres between
    passes and exact-identity deduplication would treat every pass as new.
    """
    place = facts.get("place_bucket", "")
    kind = facts.get("change_type") or facts.get("finding_kind", "")
    return f"{aoi_id}|{kind}|{place}"


def place_bucket(lon: float, lat: float, cell_deg: float = 0.004) -> str:
    """A roughly 400 m grid cell identifier, for deduplication only."""
    return f"{int(lon / cell_deg)}:{int(lat / cell_deg)}"


def evaluate(aoi: Aoi, rules: list[Rule], facts_list: list[dict],
             when: datetime | None = None,
             recent: dict[str, datetime] | None = None,
             ) -> list[RaisedAlert]:
    """Run every rule against every finding, then deduplicate and rank.

    `recent` maps a deduplication key to the last time an alert was raised for
    it, so suppression survives across pipeline runs rather than only within
    one.
    """
    at = when or datetime.now(timezone.utc)
    recent = dict(recent or {})
    raised: dict[str, RaisedAlert] = {}

    for facts in facts_list:
        matching = [r for r in rules
                    if r.applies_to(aoi) and r.matches(facts)
                    and facts.get("severity_rank", 1) >= r.severity_floor.rank]
        if not matching:
            continue

        key = _dedup_key(aoi.id, facts)
        #: The shortest window among the rules that matched. A rule written to
        #: fire often on a border sector should not be muted for a week because
        #: a broader rule also matched the same finding.
        window = min(r.suppress_days for r in matching)
        last = recent.get(key)
        if last is not None and (at - last) < timedelta(days=window):
            existing = raised.get(key)
            if existing:
                existing.occurrences += 1
            continue

        existing = raised.get(key)
        if existing is not None:
            existing.occurrences += 1
            #: Several findings can share a place and kind within one run. The
            #: alert must describe the worst of them, not whichever the change
            #: engine happened to emit first: relying on the caller's ordering
            #: means a minor surface change filed ahead of a major structure in
            #: the same bucket silently becomes the headline.
            if _outranks(facts, existing):
                _restate(existing, facts, matching)
            continue

        #: Attribute the alert to the narrowest rule that matched -- the one
        #: scoped to this AOI, else to its kind, else the general one. That is
        #: the rule an analyst asking "why did I get this" wants quoted.
        primary = min(matching, key=lambda r: (not r.aoi_ids, not r.aoi_kinds,
                                               r.suppress_days, r.id))
        severity = Severity(facts.get("severity", "low"))
        alert_id = "alert-" + format(
            seed_of(primary.id, aoi.id, key, at.date().isoformat())
            & 0xFFFFFFFFFFFF, "012x")
        alert = Alert(
            id=alert_id, org_id=primary.org_id, aoi_id=aoi.id,
            rule_id=primary.id, title=facts.get("title", primary.name),
            severity=severity, priority=4, created_at=at,
            summary=facts.get("summary", ""),
            change_event_ids=[facts["change_event_id"]]
            if facts.get("change_event_id") else [],
            anomaly_ids=[facts["anomaly_id"]] if facts.get("anomaly_id") else [],
            evidence_id=facts.get("evidence_id", ""),
            delivered_to=sorted({c for r in matching for c in r.channels}),
        )
        raised[key] = RaisedAlert(
            alert=alert, rule=primary, rules=matching, dedup_key=key,
            matched=[c.describe() for c in primary.conditions],
            risk=facts.get("risk_score"),
            held_reason=facts.get("held_reason", ""),
        )

    return prioritise(list(raised.values()))


def prioritise(alerts: list[RaisedAlert]) -> list[RaisedAlert]:
    """Assign queue positions 1..4. Position 1 is worked first.

    PRD section 20's four bands, but assigned from the composite risk score
    rather than from severity alone, so a confirmed medium-severity change at a
    remote site can outrank an unconfirmed high-severity one at a busy port --
    which is the correct ordering and the one a severity-only queue gets wrong.
    """
    ordered = sorted(
        alerts,
        key=lambda r: (-(r.risk.composite if r.risk else _fallback_score(r)),
                       -r.alert.severity.rank, r.alert.aoi_id))
    for r in ordered:
        score = r.risk.composite if r.risk else _fallback_score(r)
        if r.held_reason:
            #: Held findings never take the top of the queue. They are real and
            #: they stay visible, but sending an analyst to a maybe-container
            #: ahead of a confirmed structure is how the queue loses its meaning.
            r.alert.priority = max(3, _band(score))
        else:
            r.alert.priority = _band(score)
    return ordered


def _outranks(facts: dict, existing: RaisedAlert) -> bool:
    incoming = (facts.get("severity_rank", 1), facts.get("risk", 0.0))
    current = (existing.alert.severity.rank,
               existing.risk.composite if existing.risk else 0.0)
    return incoming > current


def _restate(alert: RaisedAlert, facts: dict, matching: list[Rule]) -> None:
    """Re-describe an alert from a more severe finding in the same bucket."""
    alert.alert.severity = Severity(facts.get("severity", "low"))
    alert.alert.title = facts.get("title", alert.alert.title)
    alert.alert.summary = facts.get("summary", alert.alert.summary)
    alert.risk = facts.get("risk_score", alert.risk)
    alert.held_reason = facts.get("held_reason", "")
    if facts.get("evidence_id"):
        alert.alert.evidence_id = facts["evidence_id"]
    if facts.get("change_event_id"):
        alert.alert.change_event_ids = [facts["change_event_id"]]
    if facts.get("anomaly_id"):
        alert.alert.anomaly_ids = [facts["anomaly_id"]]
    for rule in matching:
        if rule not in alert.rules:
            alert.rules.append(rule)


def _band(score: float) -> int:
    if score >= 75:
        return 1
    if score >= 55:
        return 2
    if score >= 35:
        return 3
    return 4


def _fallback_score(r: RaisedAlert) -> float:
    return {Severity.LOW: 25.0, Severity.MEDIUM: 50.0,
            Severity.HIGH: 75.0, Severity.CRITICAL: 92.0}[r.alert.severity]


def queue_summary(alerts: list[RaisedAlert]) -> dict:
    bands = {1: 0, 2: 0, 3: 0, 4: 0}
    for r in alerts:
        bands[r.alert.priority] = bands.get(r.alert.priority, 0) + 1
    return {
        "total": len(alerts),
        "priority_1": bands[1], "priority_2": bands[2],
        "priority_3": bands[3], "priority_4": bands[4],
        "held_for_confirmation": sum(1 for r in alerts if r.held_reason),
    }
