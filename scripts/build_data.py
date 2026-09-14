"""Build the TerraShield console dataset from a monitored database.

Reads what the pipeline actually produced -- no recomputation of findings --
and re-renders the imagery behind the most significant change at each site so
the change viewer shows real pixels rather than an illustration.

    python3 scripts/build_data.py --db terrashield.db \
        --out dashboard

The one thing this script does recompute is the raster pair for the change
viewer, because rasters are not persisted: storing every scene would make the
database hundreds of megabytes, and the provider can reproduce any scene
exactly from its id. That is a property worth having anyway -- it means an
evidence bundle's scene reference is enough to regenerate the picture.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from terrashield import change, sites                       # noqa: E402
from terrashield.catalog import SyntheticProvider, coverage  # noqa: E402
from terrashield.domain import Role, SiteStatus              # noqa: E402
from terrashield.raster import (                             # noqa: E402
    render_overlay_png, render_png, shared_stretch,
)
from terrashield.store import Store                          # noqa: E402

#: How far back a site's headline status looks. Three weeks is about four
#: Sentinel-2 revisits at these latitudes -- long enough that one cloudy
#: fortnight does not reset a site to normal, short enough that the status
#: still describes today.
CURRENT_CONDITION_DAYS = 21

STATUS_FOR = {
    SiteStatus.ANOMALOUS: "anomalous", SiteStatus.WATCH: "watch",
    SiteStatus.NORMAL: "normal", SiteStatus.STALE: "stale",
}


def provider() -> SyntheticProvider:
    p = SyntheticProvider()
    for s in sites.load_all():
        p.register(s.truth, s.climate)
    return p


def month_coverage(scenes) -> list[dict]:
    """Usable and rejected acquisitions by month. The honesty chart."""
    buckets: dict[str, dict] = {}
    for s in scenes:
        key = s.acquired_on.strftime("%Y-%m")
        b = buckets.setdefault(key, {"month": key, "usable": 0, "cloudy": 0,
                                     "sar": 0, "optical": 0})
        if s.usable:
            b["usable"] += 1
            b["sar" if s.sensor.value == "sar" else "optical"] += 1
        else:
            b["cloudy"] += 1
    return [buckets[k] for k in sorted(buckets)]


def _event_window(full, event, aoi, pad_factor: float = 1.6):
    """Cell bounds of a window around the event, with context around it."""
    from terrashield.geo import bbox, frame_for

    frame = frame_for(aoi.boundary)
    min_lon, min_lat, max_lon, max_lat = bbox(event.geometry)
    west, south = frame.to_m((min_lon, min_lat))
    east, north = frame.to_m((max_lon, max_lat))
    c0 = int((west - full.origin_e) / full.gsd_m)
    c1 = int((east - full.origin_e) / full.gsd_m)
    r0 = int((full.origin_n - north) / full.gsd_m)
    r1 = int((full.origin_n - south) / full.gsd_m)
    #: At least 60 cells of context around the finding, so a small change is
    #: not a four-pixel thumbnail with nothing around it to place it by.
    pad_c = max(30, int((c1 - c0) * pad_factor / 2))
    pad_r = max(30, int((r1 - r0) * pad_factor / 2))
    return c0 - pad_c, r0 - pad_r, c1 + pad_c, r1 + pad_r


def _event_mask(full, event, aoi):
    """The comparison's mask, restricted to this finding's own footprint.

    The full mask carries everything the threshold flagged across the AOI --
    at a working port that is twenty ships that moved overnight. Showing it
    beside a panel captioned "new structure, 3,000 m2" asks the analyst to
    guess which of the twenty highlighted things the caption is about. The
    answer is: this one.
    """
    from terrashield.geo import bbox, frame_for
    from terrashield.raster import Mask

    frame = frame_for(aoi.boundary)
    min_lon, min_lat, max_lon, max_lat = bbox(event.geometry)
    west, south = frame.to_m((min_lon, min_lat))
    east, north = frame.to_m((max_lon, max_lat))
    c0 = int((west - full.origin_e) / full.gsd_m)
    c1 = int((east - full.origin_e) / full.gsd_m)
    r0 = int((full.origin_n - north) / full.gsd_m)
    r1 = int((full.origin_n - south) / full.gsd_m)

    out = Mask(full.width, full.height, full.gsd_m, full.origin_e,
               full.origin_n, [False] * len(full.bits))
    for row in range(max(0, r0), min(full.height, r1 + 1)):
        for col in range(max(0, c0), min(full.width, c1 + 1)):
            if full.get(col, row):
                out.set(col, row, True)
    return out


def chip_set(prov, aoi, event, out_dir: Path) -> dict | None:
    """Re-render the before/after pair behind one change event, plus its mask."""
    scenes = {s.id: s for s in prov.search(
        aoi, event.detected_at.date() - timedelta(days=120),
        event.detected_at.date())}
    before = scenes.get(event.before_scene_id)
    after = scenes.get(event.after_scene_id)
    if before is None or after is None:
        return None
    rb, ra = prov.fetch(aoi, before), prov.fetch(aoi, after)
    mb, ma = prov.masks(aoi, before, rb), prov.masks(aoi, after, ra)
    result = change.compare(aoi, before, after, rb, ra, mb.cloud, ma.cloud,
                            mb.water, ma.water)
    if result.mask is None:
        return None

    stem = event.id.replace(":", "_")
    scoped = _event_mask(result.mask, event, aoi)
    window = _event_window(result.mask, event, aoi)
    #: One display range across the pair, and a second across the two crops.
    #: Stretching each panel to its own percentiles makes a sunnier day look
    #: identical to a duller one and turns a display difference into an
    #: apparent change on the ground.
    full_lo, full_hi = shared_stretch(rb, ra)
    cb, ca = rb.crop(*window), ra.crop(*window)
    det_lo, det_hi = shared_stretch(cb, ca)
    names = {}
    for label, blob in (
        ("before", render_png(rb, lo=full_lo, hi=full_hi)),
        ("after", render_png(ra, lo=full_lo, hi=full_hi)),
        ("mask", render_overlay_png(ra, scoped, lo=full_lo, hi=full_hi)),
        #: The same three, cropped to the finding. A 3,000 m2 change is thirty
        #: cells in a 420 x 320 scene -- visible only if you already know where
        #: to look, which is the one thing the analyst does not.
        ("detail_before", render_png(cb, lo=det_lo, hi=det_hi)),
        ("detail_after", render_png(ca, lo=det_lo, hi=det_hi)),
        ("detail_mask", render_overlay_png(ca, scoped.crop(*window),
                                           lo=det_lo, hi=det_hi)),
    ):
        name = f"{stem}-{label}.png"
        (out_dir / name).write_bytes(blob)
        names[label] = f"chips/{name}"
    return {
        **names,
        "before_scene": before.id,
        "after_scene": after.id,
        "before_on": before.acquired_on.isoformat(),
        "after_on": after.acquired_on.isoformat(),
        "constellation": after.constellation.value,
        "sensor": after.sensor.value,
        "gsd_m": after.gsd_m,
        "width": rb.width, "height": rb.height,
        "detail_cells": [window[2] - window[0], window[3] - window[1]],
        "registration_shift": list(result.registration_shift),
        "noise_floor": round(result.noise_floor, 5),
        "obscured_fraction": round(result.obscured_fraction, 4),
    }


def build(db_path: str, org_id: str, out: Path, as_of: date,
          chips_per_site: int = 2) -> dict:
    store = Store(db_path, org_id=org_id, actor="dashboard-build", role=Role.ADMIN)
    prov = provider()
    chips_dir = out / "chips"
    chips_dir.mkdir(parents=True, exist_ok=True)
    for old in chips_dir.glob("*.png"):
        old.unlink()

    window_start = as_of - timedelta(days=210)
    all_alerts = store.list_alerts(limit=1000)
    site_rows = []
    totals = Counter()

    for aoi in store.list_aois():
        scenes = store.list_scenes(aoi.id, window_start, as_of)
        #: Measure coverage over the period the catalogue actually covers, not
        #: over the nominal window. A site enrolled two months ago has no
        #: acquisitions from before it was enrolled, and calling that a
        #: 144-day gap without a usable look describes the enrolment date
        #: rather than the monitoring.
        observed_from = min((s.acquired_on for s in scenes), default=window_start)
        #: Each site is assessed as of the end of its own record, not as of the
        #: estate's report date. "Monitored and not seen for three weeks" and
        #: "the monitoring window for this site closed in May" are different
        #: facts, and only the first is staleness. Judging both against one
        #: global date reports a site nobody is currently watching as a site
        #: that has gone dark, which is the more alarming of the two readings
        #: and the wrong one.
        observed_to = max((s.acquired_on for s in scenes), default=as_of)
        changes = store.list_changes(aoi.id, start=window_start, end=as_of,
                                     limit=2000)
        anomalies = [a for a in store.list_anomalies(aoi.id, limit=2000)
                     if window_start <= a.observed_at.date() <= as_of]
        alerts = [a for a in all_alerts if a["aoi_id"] == aoi.id]
        cov = coverage(aoi.id, scenes, observed_from, observed_to)
        peak = max((a.score for a in anomalies), default=0.0)

        #: Status is a statement about *now*, not about whether anything ever
        #: happened. Computed over the whole window every site that had one
        #: interesting fortnight in three months reads "anomalous" for the
        #: remaining ten weeks, and a status field that is the same on every
        #: row tells an operations manager nothing about where to look today.
        recent_from = observed_to - timedelta(days=CURRENT_CONDITION_DAYS)
        recent_alerts = [a for a in alerts
                         if a["created_at"][:10] >= recent_from.isoformat()]
        recent_peak = max((a.score for a in anomalies
                           if a.observed_at.date() >= recent_from), default=0.0)
        status = (SiteStatus.ANOMALOUS if recent_peak >= 65
                  or any(a["priority"] <= 2 for a in recent_alerts)
                  else SiteStatus.WATCH if recent_alerts or recent_peak >= 40
                  else SiteStatus.NORMAL)
        if cov.last_usable is None or (observed_to - cov.last_usable).days > 21:
            #: Acquisitions are arriving and none of them is usable. That is
            #: the real stale case: the constellation is looking and the
            #: weather is winning.
            status = SiteStatus.STALE

        notable = sorted(changes,
                         key=lambda e: (-e.severity.rank, -e.confidence, -e.area_m2))
        chips = []
        for event in notable[:chips_per_site]:
            c = chip_set(prov, aoi, event, chips_dir)
            if c:
                chips.append({
                    "change_id": event.id,
                    "type": event.change_type.value,
                    "severity": event.severity.value,
                    "confidence": event.confidence,
                    "area_m2": event.area_m2,
                    "detected_on": event.detected_at.date().isoformat(),
                    "explanation": event.explanation,
                    "evidence_id": event.evidence_id,
                    **c,
                })

        demo = next((d for d in sites.load_all() if d.id == aoi.id), None)
        totals["changes"] += len(changes)
        totals["alerts"] += len(alerts)
        totals["objects"] += len(store.list_detections(aoi.id, limit=100000))

        site_rows.append({
            "id": aoi.id, "name": aoi.name, "kind": aoi.kind.value,
            "country": aoi.country, "description": aoi.description,
            "area_km2": round(aoi.area_km2, 1),
            "centroid": [round(v, 5) for v in aoi.centroid],
            "bbox": [round(v, 5) for v in aoi.bbox],
            "fingerprint": aoi.fingerprint,
            "headline": demo.headline if demo else "",
            "monitored_from": observed_from.isoformat(),
            "monitored_to": observed_to.isoformat(),
            "status": STATUS_FOR[status],
            "status_window_days": CURRENT_CONDITION_DAYS,
            "peak_anomaly_score": round(peak, 1),
            "recent_anomaly_score": round(recent_peak, 1),
            "recent_alert_count": len(recent_alerts),
            "coverage": cov.to_dict(),
            "coverage_by_month": month_coverage(scenes),
            "change_count": len(changes),
            "alert_count": len(alerts),
            "changes": [
                {"id": e.id, "type": e.change_type.value,
                 "detected_on": e.detected_at.date().isoformat(),
                 "area_m2": e.area_m2, "confidence": e.confidence,
                 "severity": e.severity.value, "explanation": e.explanation,
                 "evidence_id": e.evidence_id,
                 "review_status": e.review_status.value}
                for e in notable[:40]],
            "anomalies": [
                {"date": a.observed_at.date().isoformat(), "score": a.score,
                 "confidence": a.confidence, "severity": a.severity.value,
                 "baseline_n": a.baseline_n, "reasons": a.reasons[:3]}
                for a in sorted(anomalies, key=lambda x: x.observed_at)],
            "chips": chips,
        })

    queue = sorted(all_alerts, key=lambda a: (a["priority"], a["created_at"]))
    intact, bad = store.verify_audit_chain()

    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of": as_of.isoformat(),
        "organisation": org_id,
        "window_days": 210,
        "totals": {
            "sites": len(site_rows),
            "alerts": len(all_alerts),
            "priority_1": sum(1 for a in all_alerts if a["priority"] == 1),
            "priority_2": sum(1 for a in all_alerts if a["priority"] == 2),
            "changes": totals["changes"],
            "objects": totals["objects"],
            "usable_scenes": sum(s["coverage"]["usable"] for s in site_rows),
            "acquired_scenes": sum(s["coverage"]["acquired"] for s in site_rows),
        },
        "audit": {"chain_intact": intact, "first_bad_entry": bad,
                  "entries": store.counts().get("audit_log", 0)},
        "sites": site_rows,
        "queue": [
            {"id": a["id"], "aoi_id": a["aoi_id"], "title": a["title"],
             "severity": a["severity"], "priority": a["priority"],
             "created_at": a["created_at"], "summary": a["summary"],
             "evidence_id": a["evidence_id"],
             "rule": (a.get("payload") or {}).get("rule_name", ""),
             "rule_text": (a.get("payload") or {}).get("rule", ""),
             "matched_rules": (a.get("payload") or {}).get("matched_rules", []),
             "risk": (a.get("payload") or {}).get("risk") or {},
             "held": (a.get("payload") or {}).get("held_for_confirmation", ""),
             "review_status": a["review_status"]}
            for a in queue[:60]],
        "rules": [r.to_dict() for r in store.list_rules(enabled_only=False)],
    }
    return doc


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="terrashield.db")
    p.add_argument("--org", default=sites.DEMO_ORG)
    p.add_argument("--out", default="dashboard")
    p.add_argument("--as-of", default="")
    p.add_argument("--chips", type=int, default=2)
    args = p.parse_args(argv)

    as_of = (date.fromisoformat(args.as_of) if args.as_of
             else datetime.now(timezone.utc).date())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    doc = build(args.db, args.org, out, as_of, args.chips)
    (out / "data.json").write_text(json.dumps(doc, indent=1))
    size = (out / "data.json").stat().st_size
    print(f"wrote {out / 'data.json'} ({size / 1024:.0f} KB)")
    print(f"  {doc['totals']['sites']} sites, {doc['totals']['changes']} changes, "
          f"{doc['totals']['alerts']} alerts, "
          f"{len(list((out / 'chips').glob('*.png')))} imagery chips")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
