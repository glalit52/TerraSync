"""Intelligence reports: daily, weekly and per-site.

PRD section 23. The format is markdown because a report's job is to leave the
system -- into a briefing pack, an email, a document -- and markdown survives
that trip in a way a rendered dashboard does not.

Every report opens with coverage rather than with findings. That ordering is
deliberate and it is the single most important editorial decision here: a
weekly report that begins "3 changes detected" reads as a quiet week, and a
reader has no way to tell it apart from a week in which the site was under
cloud for six days and was effectively unmonitored. Leading with what was seen
makes the difference between a quiet week and a blind one impossible to miss.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .domain import Aoi, Report, Severity, SiteStatus
from .store.repo import Store

SEVERITY_ORDER = [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW]


def _coverage_table(store: Store, aois: list[Aoi], start: date,
                    end: date) -> list[str]:
    lines = ["| Area | Acquired | Usable | Last usable | Longest gap |",
             "|---|---:|---:|---|---:|"]
    for aoi in aois:
        scenes = store.list_scenes(aoi.id, start, end)
        usable = sorted({s.acquired_on for s in scenes if s.usable})
        gap, prev = 0, start
        for d in usable:
            gap = max(gap, (d - prev).days)
            prev = d
        gap = max(gap, (end - prev).days)
        lines.append(
            f"| {aoi.name} | {len(scenes)} | {len([s for s in scenes if s.usable])} "
            f"| {usable[-1].isoformat() if usable else 'never'} | {gap} d |")
    return lines


def daily(store: Store, org_id: str, when: date) -> Report:
    """What happened yesterday, and what an analyst should look at today."""
    aois = store.list_aois()
    alerts = [a for a in store.list_alerts(limit=200)
              if a["created_at"][:10] == when.isoformat()]
    changes = store.list_changes(start=when, end=when, limit=200)

    body = [
        f"# Daily intelligence summary — {when.isoformat()}",
        "",
        f"{len(aois)} area(s) monitored. {len(alerts)} alert(s) raised, "
        f"{len(changes)} change event(s) recorded.",
        "",
        "## Coverage",
        "",
        "What the constellation actually delivered. Read this before the "
        "findings: a quiet day and an unobserved day look identical further "
        "down.",
        "",
    ]
    body += _coverage_table(store, aois, when - timedelta(days=7), when)

    body += ["", "## Alert queue", ""]
    if not alerts:
        body.append("No alerts were raised.")
    else:
        body += ["| Priority | Area | Finding | Severity | Confidence |",
                 "|---:|---|---|---|---:|"]
        for a in sorted(alerts, key=lambda x: x["priority"]):
            payload = a.get("payload", {})
            risk = (payload.get("risk") or {})
            body.append(
                f"| {a['priority']} | {a['aoi_id']} | {a['title']} "
                f"| {a['severity']} | {risk.get('confidence', 0):.0%} |")
        held = [a for a in alerts if a.get("payload", {}).get("held_for_confirmation")]
        if held:
            body += ["", f"{len(held)} finding(s) are held for confirmation "
                         "against the next usable scene: a single look cannot "
                         "separate a moveable object from new construction."]

    body += ["", "## Changes", ""]
    if not changes:
        body.append("No change events were recorded.")
    else:
        for e in sorted(changes, key=lambda x: -x.severity.rank)[:12]:
            body.append(
                f"- **{e.change_type.value.replace('_', ' ')}** at {e.aoi_id}, "
                f"{e.area_m2:,.0f} m2, {e.severity.value}, confidence "
                f"{e.confidence:.0%} — {e.explanation}")

    body += ["", _footer()]
    return _report(org_id, "daily", org_id, when, when, "\n".join(body),
                   [a["id"] for a in alerts])


def weekly(store: Store, org_id: str, end: date) -> Report:
    """Seven days: what persisted, what was new, and what was not seen."""
    start = end - timedelta(days=6)
    aois = store.list_aois()
    alerts = [a for a in store.list_alerts(limit=500)
              if start.isoformat() <= a["created_at"][:10] <= end.isoformat()]
    changes = store.list_changes(start=start, end=end, limit=800)

    by_sev: dict[str, int] = {}
    for e in changes:
        by_sev[e.severity.value] = by_sev.get(e.severity.value, 0) + 1

    body = [
        f"# Weekly intelligence summary — {start.isoformat()} to {end.isoformat()}",
        "",
        f"{len(aois)} area(s) monitored. {len(changes)} change event(s), "
        f"{len(alerts)} alert(s).",
        "",
        "## Coverage",
        "",
    ]
    body += _coverage_table(store, aois, start, end)

    body += ["", "## By area", "",
             "| Area | Status | Changes | Alerts | Peak anomaly |",
             "|---|---|---:|---:|---:|"]
    for aoi in aois:
        site_changes = [e for e in changes if e.aoi_id == aoi.id]
        site_alerts = [a for a in alerts if a["aoi_id"] == aoi.id]
        anomalies = [a for a in store.list_anomalies(aoi.id, limit=400)
                     if start <= a.observed_at.date() <= end]
        peak = max((a.score for a in anomalies), default=0.0)
        status = (SiteStatus.ANOMALOUS if peak >= 65 or
                  any(a["priority"] <= 2 for a in site_alerts)
                  else SiteStatus.WATCH if site_alerts or peak >= 40
                  else SiteStatus.NORMAL)
        body.append(f"| {aoi.name} | {status.value} | {len(site_changes)} "
                    f"| {len(site_alerts)} | {peak:.0f} |")

    body += ["", "## Significant changes", ""]
    notable = [e for e in changes if e.severity.rank >= Severity.MEDIUM.rank]
    if not notable:
        body.append("Nothing above routine.")
    for e in sorted(notable, key=lambda x: (-x.severity.rank, -x.area_m2))[:15]:
        body.append(
            f"- **{e.aoi_id}** — {e.change_type.value.replace('_', ' ')}, "
            f"{e.area_m2:,.0f} m2 on {e.detected_at.date().isoformat()} "
            f"({e.severity.value}, confidence {e.confidence:.0%}). "
            f"{e.explanation}"
            + (f" Evidence: `{e.evidence_id}`." if e.evidence_id else ""))

    body += ["", "## Severity distribution", ""]
    for sev in SEVERITY_ORDER:
        body.append(f"- {sev.value}: {by_sev.get(sev.value, 0)}")

    body += ["", _footer()]
    return _report(org_id, "weekly", org_id, start, end, "\n".join(body),
                   [a["id"] for a in alerts])


def site(store: Store, aoi: Aoi, start: date, end: date) -> Report:
    """One area in depth: condition, timeline, findings, evidence."""
    scenes = store.list_scenes(aoi.id, start, end)
    usable = [s for s in scenes if s.usable]
    changes = store.list_changes(aoi.id, start=start, end=end, limit=500)
    anomalies = [a for a in store.list_anomalies(aoi.id, limit=800)
                 if start <= a.observed_at.date() <= end]
    #: Filtered by the report's period like everything else around it. It was
    #: not, so a site report for March counted every alert the site had ever
    #: raised and quietly disagreed with the timeline printed underneath it.
    alerts = [a for a in store.list_alerts(aoi.id, limit=500)
              if start.isoformat() <= a["created_at"][:10] <= end.isoformat()]
    peak = max((a.score for a in anomalies), default=0.0)

    coverage_line = (
        f"- Usable acquisitions: {len(usable)} of {len(scenes)} "
        f"({len(usable) / len(scenes):.0%})" if scenes
        else "- No acquisitions at all in this period — nothing below is a "
             "statement about the ground")

    body = [
        f"# Site report — {aoi.name}",
        "",
        f"**Area** {aoi.id} · {aoi.kind.value.replace('_', ' ')} · "
        f"{aoi.area_km2:,.1f} km2 · boundary fingerprint `{aoi.fingerprint}`",
        f"**Period** {start.isoformat()} to {end.isoformat()}",
        "",
        "## Current condition",
        "",
        coverage_line,
        f"- Change events: {len(changes)}",
        f"- Alerts: {len(alerts)}",
        f"- Peak anomaly score: {peak:.0f}/100",
        "",
    ]

    by_sensor: dict[str, int] = {}
    for s in usable:
        by_sensor[s.constellation.value] = by_sensor.get(s.constellation.value, 0) + 1
    if by_sensor:
        body += ["## Sensors used", ""]
        for k, v in sorted(by_sensor.items(), key=lambda kv: -kv[1]):
            body.append(f"- {k}: {v} usable acquisition(s)")
        sar = sum(v for k, v in by_sensor.items() if "sentinel-1" in k)
        if sar and sar / max(len(usable), 1) > 0.5:
            body += ["", "More than half the usable record here is radar. "
                         "Optical coverage at this site is limited by cloud, "
                         "and the change record leans on SAR accordingly."]
        body.append("")

    body += ["## Timeline", ""]
    if not changes:
        body.append("No changes recorded in this period.")
    else:
        for e in sorted(changes, key=lambda x: x.detected_at):
            body.append(
                f"- `{e.detected_at.date().isoformat()}` "
                f"**{e.change_type.value.replace('_', ' ')}** — "
                f"{e.area_m2:,.0f} m2, {e.severity.value}, confidence "
                f"{e.confidence:.0%}. {e.explanation}")

    if anomalies:
        body += ["", "## Activity against baseline", ""]
        for a in sorted(anomalies, key=lambda x: -x.score)[:8]:
            body.append(
                f"- `{a.observed_at.date().isoformat()}` score {a.score:.0f}/100 "
                f"at {a.confidence:.0%} confidence (baseline n={a.baseline_n})"
                + (": " + "; ".join(a.reasons[:2]) if a.reasons else ""))
        body += ["", "These are deviations from this site's own record. They "
                     "describe how unusual an observation is, not why."]

    evidence_ids = [e.evidence_id for e in changes if e.evidence_id]
    if evidence_ids:
        body += ["", "## Evidence", "",
                 f"{len(evidence_ids)} evidence bundle(s) support the findings "
                 "above. Each lists the scenes compared, the mask produced, the "
                 "model version and what could not be established.", ""]
        for eid in evidence_ids[:12]:
            body.append(f"- `{eid}`")

    body += ["", _footer()]
    return _report(aoi.org_id, "site", aoi.id, start, end, "\n".join(body),
                   [a["id"] for a in alerts])


def _footer() -> str:
    return ("---\n\nGenerated by TerraShield. Findings describe observed change "
            "and statistical deviation from each site's own history. They carry "
            "no assessment of cause, ownership or intent, and every one is "
            "subject to analyst review.")


def _report(org_id: str, kind: str, subject: str, start: date, end: date,
            body: str, alert_ids: list[str]) -> Report:
    return Report(
        id=f"rep-{kind}-{subject}-{end.isoformat()}"[:120],
        org_id=org_id, kind=kind, subject=subject,
        period_start=start, period_end=end,
        generated_at=datetime.now(timezone.utc),
        body_markdown=body, alert_ids=alert_ids)
