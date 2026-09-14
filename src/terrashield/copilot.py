"""The analyst copilot: questions answered from the record, with citations.

PRD section 21 wants conversational interrogation of the map and section 22
insists every conclusion link back to evidence. Those two pull in opposite
directions if the copilot is a language model over free text, because the
fluent answer and the defensible answer are not the same answer, and the fluent
one is what gets repeated in a briefing.

So the design here is the other way round. This module is a *grounding layer*:
it parses a question into a typed query, runs that query against the store, and
returns a structured `Answer` -- numbers, the rows they came from, and the
evidence ids behind them. Every sentence it produces is generated from a value
it just read. It cannot state something the database does not contain, because
there is no path in the code from a question to a sentence that does not go
through a query.

A language model belongs on top of this, turning `Answer` into prose and
handling the phrasings the parser misses. That is the right division of
labour: the model does language, the store does facts, and the citations make
the join checkable. Putting the model underneath instead -- letting it read raw
text and summarise -- is what produces a confident, fluent, unattributable
claim about a border post, which is the single worst output this product could
produce.

`Answer.unsupported` is the other half of the contract. When the question is
outside what the data can answer, the copilot says so and says why, rather than
reaching for the nearest thing it can compute.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .domain import Aoi, ChangeType, Severity
from .store.repo import Store


@dataclass
class Citation:
    kind: str            # change | anomaly | alert | scene | evidence | coverage
    ref: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "ref": self.ref, "detail": self.detail}


@dataclass
class Answer:
    question: str
    intent: str
    text: str
    rows: list[dict] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    unsupported: str = ""

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "intent": self.intent,
            "answer": self.text,
            "rows": self.rows,
            "citations": [c.to_dict() for c in self.citations],
            "unsupported": self.unsupported,
        }


#: Questions this copilot must refuse rather than approximate. Each one asks
#: for an attribution the imagery cannot support, and answering any of them --
#: even hedged -- would put an assessment of intent into a system whose entire
#: credibility rests on not making one.
REFUSALS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b(whose|who owns|who is responsible|who built|which country)\b", re.I),
     "attribution of ownership or responsibility cannot be derived from imagery; "
     "this system reports what changed, where and when"),
    (re.compile(r"\b(threat|hostile|enemy|attack|intent|intention|planning)\b", re.I),
     "this system measures deviation from a site's own history and does not "
     "assess intent. What it can tell you is what changed, by how much, and how "
     "unusual that is for this location"),
    (re.compile(r"\b(target|targeting|strike|engage|weapon)\b", re.I),
     "TerraShield is an analyst-assistance and monitoring system. It does not "
     "support targeting or engagement workflows"),
    (re.compile(r"\b(predict|forecast|will happen|going to happen)\b", re.I),
     "no forecasting model is in use. Trends over the observed window are "
     "available and are described as trends, not predictions"),
)


def _period(question: str, today: date) -> tuple[date, date, str]:
    q = question.lower()
    m = re.search(r"last\s+(\d+)\s*(day|week|month)", q)
    if m:
        n = int(m.group(1))
        days = {"day": n, "week": 7 * n, "month": 30 * n}[m.group(2)]
        return today - timedelta(days=days), today, f"the last {n} {m.group(2)}(s)"
    if "this week" in q or "past week" in q:
        return today - timedelta(days=7), today, "the last 7 days"
    if "this month" in q or "past month" in q:
        return today - timedelta(days=30), today, "the last 30 days"
    if "six months" in q or "6 months" in q:
        return today - timedelta(days=182), today, "the last six months"
    if "year" in q:
        return today - timedelta(days=365), today, "the last year"
    return today - timedelta(days=30), today, "the last 30 days"


def _resolve_aoi(store: Store, question: str) -> Aoi | None:
    """Find the AOI a question is about by id, name or a distinctive word."""
    q = question.lower()
    aois = store.list_aois()
    for a in aois:
        if a.id.lower() in q:
            return a
    best: tuple[int, Aoi] | None = None
    for a in aois:
        words = [w for w in re.split(r"[^a-z0-9]+", a.name.lower()) if len(w) > 3]
        hits = sum(1 for w in words if w in q)
        if hits and (best is None or hits > best[0]):
            best = (hits, a)
    return best[1] if best else None


def ask(store: Store, question: str, today: date | None = None,
        default_aoi: Aoi | None = None) -> Answer:
    """Answer one question from the stored record."""
    today = today or datetime.now(timezone.utc).date()
    for pattern, why in REFUSALS:
        if pattern.search(question):
            return Answer(question=question, intent="refused",
                          text="", unsupported=why)

    store.audit("copilot.ask", "copilot", {"question": question})
    q = question.lower()
    aoi = _resolve_aoi(store, question) or default_aoi
    start, end, label = _period(question, today)

    #: Prefixes, not whole words. `\banomal\b` matches nothing an analyst
    #: would actually type -- not "anomaly", not "anomalous" -- and the router
    #: silently falls through to a generic site summary for every question
    #: about change. Trailing `\w*` is what makes "changed", "changes" and
    #: "construction" all reach the handler they belong to.
    if re.search(r"\b(highest|worst|most|top|rank|which (areas|sites))\w*", q) \
            and re.search(r"\b(anomal|score|risk|attention|priorit)\w*", q):
        return _rank_sites(store, question, start, end, label)
    if re.search(r"\b(coverage|how often|revisit|last (seen|imaged|look)|gap)\w*", q):
        return _coverage(store, question, aoi, start, end, label)
    if re.search(r"\b(alert|priorit|queue|needs? attention|today)\w*", q) and aoi is None:
        return _alert_queue(store, question, label)
    if aoi is None:
        return Answer(
            question=question, intent="unresolved", text="",
            unsupported=("the question does not name a monitored area. Name one "
                         "of: " + ", ".join(a.name for a in store.list_aois())))
    if re.search(r"\b(chang|new|construct|built|appear|differ|compar)\w*", q):
        return _changes(store, question, aoi, start, end, label)
    if re.search(r"\b(anomal|unusual|normal|baseline|deviat|activit)\w*", q):
        return _anomalies(store, question, aoi, start, end, label)
    return _site_summary(store, question, aoi, start, end, label)


def _changes(store: Store, question: str, aoi: Aoi, start: date, end: date,
             label: str) -> Answer:
    events = store.list_changes(aoi.id, start=start, end=end)
    wanted = [t for t in ChangeType if t.value.replace("_", " ") in question.lower()]
    if wanted:
        events = [e for e in events if e.change_type in wanted]
    if not events:
        return Answer(
            question=question, intent="changes",
            text=(f"No changes were recorded at {aoi.name} over {label}. Note "
                  "that this means none were detected in the imagery that was "
                  "usable, not that none occurred -- check coverage for the same "
                  "period before reading it as quiet."),
            citations=[Citation("coverage", aoi.id, label)])

    by_type: dict[str, int] = {}
    for e in events:
        by_type[e.change_type.value] = by_type.get(e.change_type.value, 0) + 1
    notable = [e for e in events if e.severity.rank >= Severity.MEDIUM.rank][:5]
    parts = [f"{n} {k.replace('_', ' ')}" for k, n in
             sorted(by_type.items(), key=lambda kv: -kv[1])]
    text = (f"{len(events)} change{'s' if len(events) != 1 else ''} recorded at "
            f"{aoi.name} over {label}: " + ", ".join(parts) + ".")
    if notable:
        text += " The most significant: " + "; ".join(
            f"{e.change_type.value.replace('_', ' ')} of {e.area_m2:,.0f} m2 on "
            f"{e.detected_at.date().isoformat()} ({e.severity.value}, "
            f"confidence {e.confidence:.0%})" for e in notable) + "."
    return Answer(
        question=question, intent="changes", text=text,
        rows=[{"id": e.id, "type": e.change_type.value,
               "detected_on": e.detected_at.date().isoformat(),
               "area_m2": e.area_m2, "severity": e.severity.value,
               "confidence": e.confidence, "explanation": e.explanation,
               "evidence_id": e.evidence_id} for e in events],
        citations=[Citation("change", e.id, e.explanation[:90]) for e in events[:8]]
        + [Citation("evidence", e.evidence_id, "derivation")
           for e in events[:8] if e.evidence_id])


def _anomalies(store: Store, question: str, aoi: Aoi, start: date, end: date,
               label: str) -> Answer:
    rows = [a for a in store.list_anomalies(aoi.id, limit=500)
            if start <= a.observed_at.date() <= end]
    if not rows:
        return Answer(question=question, intent="anomalies",
                      text=f"No anomaly assessments are recorded for "
                           f"{aoi.name} over {label}.")
    rows.sort(key=lambda a: -a.score)
    top = rows[0]
    elevated = [a for a in rows if a.score >= 40]
    text = (f"{aoi.name} was assessed on {len(rows)} day(s) over {label}. "
            f"{len(elevated)} day(s) scored above the routine range. The largest "
            f"deviation was {top.score:.0f}/100 on "
            f"{top.observed_at.date().isoformat()} at "
            f"{top.confidence:.0%} confidence, against a baseline of "
            f"{top.baseline_n} prior observations.")
    if top.reasons:
        text += " Drivers: " + "; ".join(top.reasons[:3]) + "."
    text += (" These are statistical deviations from this site's own record and "
             "carry no assessment of cause.")
    return Answer(
        question=question, intent="anomalies", text=text,
        rows=[{"id": a.id, "date": a.observed_at.date().isoformat(),
               "score": a.score, "confidence": a.confidence,
               "severity": a.severity.value, "baseline_n": a.baseline_n,
               "reasons": a.reasons} for a in rows[:20]],
        citations=[Citation("anomaly", a.id,
                            f"score {a.score:.0f} on "
                            f"{a.observed_at.date().isoformat()}")
                   for a in rows[:6]])


def _rank_sites(store: Store, question: str, start: date, end: date,
                label: str) -> Answer:
    ranked: list[dict] = []
    for aoi in store.list_aois():
        rows = [a for a in store.list_anomalies(aoi.id, limit=500)
                if start <= a.observed_at.date() <= end]
        alerts = [a for a in store.list_alerts(aoi.id, limit=500)
                  if a["created_at"][:10] >= start.isoformat()]
        peak = max((a.score for a in rows), default=0.0)
        ranked.append({
            "aoi_id": aoi.id, "name": aoi.name, "kind": aoi.kind.value,
            "peak_anomaly_score": round(peak, 1),
            "assessments": len(rows), "alerts": len(alerts),
            "priority_1_alerts": sum(1 for a in alerts if a["priority"] == 1),
        })
    ranked.sort(key=lambda r: (-r["priority_1_alerts"], -r["peak_anomaly_score"]))
    if not ranked:
        return Answer(question=question, intent="rank", text="",
                      unsupported="no monitored areas are configured")
    lines = [f"{r['name']}: peak anomaly {r['peak_anomaly_score']:.0f}/100, "
             f"{r['alerts']} alert(s), {r['priority_1_alerts']} at priority 1"
             for r in ranked]
    return Answer(
        question=question, intent="rank",
        text=f"Monitored areas ranked by attention needed over {label} — "
             + "; ".join(lines) + ".",
        rows=ranked,
        citations=[Citation("anomaly", r["aoi_id"], r["name"]) for r in ranked])


def _alert_queue(store: Store, question: str, label: str) -> Answer:
    rows = store.list_alerts(limit=100)
    if not rows:
        return Answer(question=question, intent="queue",
                      text="The alert queue is empty.")
    bands: dict[int, int] = {}
    for r in rows:
        bands[r["priority"]] = bands.get(r["priority"], 0) + 1
    head = rows[:5]
    text = (f"{len(rows)} open alert(s): "
            + ", ".join(f"{n} at priority {p}" for p, n in sorted(bands.items()))
            + ". Work first: "
            + "; ".join(f"{r['title']} at {r['aoi_id']} ({r['severity']})"
                        for r in head) + ".")
    return Answer(question=question, intent="queue", text=text, rows=rows[:20],
                  citations=[Citation("alert", r["id"], r["title"]) for r in head])


def _coverage(store: Store, question: str, aoi: Aoi | None, start: date,
              end: date, label: str) -> Answer:
    targets = [aoi] if aoi else store.list_aois()
    rows = []
    for a in targets:
        scenes = store.list_scenes(a.id, start, end)
        usable = [s for s in scenes if s.usable]
        last = max((s.acquired_on for s in usable), default=None)
        by_sensor: dict[str, int] = {}
        for s in usable:
            by_sensor[s.sensor.value] = by_sensor.get(s.sensor.value, 0) + 1
        rows.append({
            "aoi_id": a.id, "name": a.name, "acquired": len(scenes),
            "usable": len(usable),
            "usable_fraction": round(len(usable) / len(scenes), 3) if scenes else 0,
            "last_usable": last.isoformat() if last else None,
            "by_sensor": by_sensor,
        })
    text = f"Imagery coverage over {label}: " + "; ".join(
        f"{r['name']} — {r['usable']} usable of {r['acquired']} acquired "
        f"({r['usable_fraction']:.0%}), last usable "
        f"{r['last_usable'] or 'never'}"
        + (f", {r['by_sensor'].get('sar', 0)} of them SAR"
           if r["by_sensor"].get("sar") else "")
        for r in rows) + "."
    return Answer(question=question, intent="coverage", text=text, rows=rows,
                  citations=[Citation("coverage", r["aoi_id"], label) for r in rows])


def _site_summary(store: Store, question: str, aoi: Aoi, start: date, end: date,
                  label: str) -> Answer:
    changes = store.list_changes(aoi.id, start=start, end=end)
    anomalies = [a for a in store.list_anomalies(aoi.id, limit=500)
                 if start <= a.observed_at.date() <= end]
    alerts = store.list_alerts(aoi.id, limit=100)
    scenes = store.list_scenes(aoi.id, start, end, usable_only=True)
    peak = max((a.score for a in anomalies), default=0.0)
    text = (f"{aoi.name} ({aoi.kind.value.replace('_', ' ')}, "
            f"{aoi.area_km2:,.0f} km2) over {label}: {len(scenes)} usable "
            f"acquisition(s), {len(changes)} change event(s), "
            f"{len(alerts)} alert(s), peak anomaly score {peak:.0f}/100.")
    if changes:
        worst = max(changes, key=lambda e: (e.severity.rank, e.area_m2))
        text += (f" Most significant change: "
                 f"{worst.change_type.value.replace('_', ' ')} of "
                 f"{worst.area_m2:,.0f} m2 on "
                 f"{worst.detected_at.date().isoformat()} — {worst.explanation}")
    return Answer(
        question=question, intent="site", text=text,
        rows=[{"aoi_id": aoi.id, "usable_scenes": len(scenes),
               "changes": len(changes), "alerts": len(alerts),
               "peak_anomaly_score": round(peak, 1)}],
        citations=[Citation("change", e.id, e.change_type.value)
                   for e in changes[:5]]
        + [Citation("alert", a["id"], a["title"]) for a in alerts[:3]])
