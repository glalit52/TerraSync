"""Evidence: what a finding rests on, in a form somebody else can check.

PRD section 22 calls this extremely important and it is, but the reason is
narrower than "explainability". An intelligence product gets challenged. Someone
senior asks why the system said a structure appeared at a border post, and the
answer has to be a list of specific artefacts -- these two scenes, acquired on
these dates from this constellation, this change mask, this baseline, this model
version -- not a paragraph about how the model works.

So an `EvidenceBundle` is not a log line. It is the set of things a second
analyst would need to reproduce the finding, each with a content hash, and the
bundle's own hash over all of them. That makes three separate claims checkable:

  * the finding came from the inputs it says it did (hashes match)
  * nothing has been edited since (bundle hash matches)
  * the same inputs through the same model version give the same answer

The bundle also records what was *not* available -- the cloud that masked a
third of the AOI, the class the sensor could not resolve, the baseline that was
too short. A finding whose evidence lists its own gaps is far more credible than
one that does not, and in a review it is the gaps that get asked about first.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from .domain import AnomalyFinding, Aoi, ChangeEvent, Scene


def digest(*parts: Any) -> str:
    blob = json.dumps(parts, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


@dataclass(frozen=True)
class Artefact:
    """One checkable input or intermediate product."""

    kind: str          # scene | mask | baseline | model | parameter | derived
    ref: str           # scene id, metric name, model version, file path
    sha256: str
    note: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "ref": self.ref,
                "sha256": self.sha256, "note": self.note}


@dataclass
class EvidenceBundle:
    """Everything behind one finding, hashed."""

    id: str
    aoi_id: str
    finding_id: str
    finding_kind: str            # change | anomaly | alert
    created_at: datetime
    #: When the finding itself happened. Distinct from `created_at`, which is
    #: when this bundle was written: a pack covering March, exported in
    #: September, has to select on the first and not the second.
    finding_at: datetime | None = None
    artefacts: list[Artefact] = field(default_factory=list)
    measurements: dict[str, Any] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)
    narrative: str = ""

    def add(self, kind: str, ref: str, payload: Any, note: str = "") -> None:
        self.artefacts.append(Artefact(kind, ref, digest(payload), note))

    @property
    def sha256(self) -> str:
        """Hash over every artefact, measurement and gap, in a fixed order."""
        return digest(
            self.aoi_id, self.finding_id, self.finding_kind,
            [a.to_dict() for a in self.artefacts],
            sorted(self.measurements.items(), key=lambda kv: kv[0]),
            sorted(self.gaps),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "aoi_id": self.aoi_id,
            "finding_id": self.finding_id,
            "finding_kind": self.finding_kind,
            "created_at": self.created_at.isoformat(),
            "finding_at": self.finding_at.isoformat() if self.finding_at else None,
            "sha256": self.sha256,
            "artefacts": [a.to_dict() for a in self.artefacts],
            "measurements": self.measurements,
            "gaps": self.gaps,
            "narrative": self.narrative,
        }

    def render(self) -> str:
        """The bundle as text an analyst can paste into a briefing."""
        lines = [
            f"EVIDENCE {self.id}",
            f"  finding      {self.finding_kind} {self.finding_id}",
            f"  area         {self.aoi_id}",
            f"  observed     {self.finding_at.isoformat()}"
            if self.finding_at else "  observed     not recorded",
            f"  produced     {self.created_at.isoformat()}",
            f"  bundle hash  {self.sha256[:32]}",
            "",
            "  based on",
        ]
        for a in self.artefacts:
            note = f"  -- {a.note}" if a.note else ""
            lines.append(f"    {a.kind:<10} {a.ref}{note}")
            lines.append(f"    {'':<10} sha256 {a.sha256[:32]}")
        if self.measurements:
            lines += ["", "  measured"]
            for k, v in sorted(self.measurements.items()):
                shown = f"{v:,.4g}" if isinstance(v, (int, float)) else v
                lines.append(f"    {k.replace('_', ' '):<28} {shown}")
        if self.gaps:
            lines += ["", "  not established"]
            for g in self.gaps:
                lines.append(f"    - {g}")
        if self.narrative:
            lines += ["", "  reading", f"    {self.narrative}"]
        return "\n".join(lines)


def _scene_payload(s: Scene) -> dict:
    return {
        "id": s.id, "constellation": s.constellation.value,
        "sensor": s.sensor.value, "acquired_at": s.acquired_at.isoformat(),
        "gsd_m": s.gsd_m, "cloud_pct": s.cloud_pct,
        "sun_elevation_deg": s.sun_elevation_deg, "orbit": s.orbit,
    }


def for_change(event: ChangeEvent, aoi: Aoi, before: Scene, after: Scene,
               result, baselines: dict | None = None) -> EvidenceBundle:
    """Assemble the evidence behind one change event."""
    bundle = EvidenceBundle(
        id="ev-" + digest(event.id, aoi.fingerprint)[:16],
        aoi_id=aoi.id, finding_id=event.id, finding_kind="change",
        created_at=datetime.now(timezone.utc), finding_at=event.detected_at,
        narrative=event.explanation,
    )
    bundle.add("scene", before.id, _scene_payload(before),
               f"before, {before.constellation.value}, "
               f"{before.cloud_pct:.0f}% cloud, sun {before.sun_elevation_deg:.0f} deg")
    bundle.add("scene", after.id, _scene_payload(after),
               f"after, {after.constellation.value}, "
               f"{after.cloud_pct:.0f}% cloud, sun {after.sun_elevation_deg:.0f} deg")
    bundle.add("mask", f"change-mask:{event.id}",
               {"geometry": event.geometry, "area_m2": event.area_m2},
               f"{event.area_m2:,.0f} m2 flagged above the noise floor")
    bundle.add("model", event.model_version, {"version": event.model_version},
               "change engine version that produced this polygon")
    bundle.add("parameter", "aoi-boundary",
               {"fingerprint": aoi.fingerprint, "boundary": aoi.boundary},
               "the exact footprint analysed; a redrawn boundary invalidates "
               "the comparison")

    bundle.measurements.update({
        "change_type": event.change_type.value,
        "area_m2": event.area_m2,
        "radiometric_magnitude": event.magnitude,
        "confidence": event.confidence,
        "severity": event.severity.value,
        "days_between_scenes": (after.acquired_on - before.acquired_on).days,
        "noise_floor": round(result.noise_floor, 5),
        "registration_shift_cells": list(result.registration_shift),
        "radiometric_gain": round(result.gain, 4),
        "obscured_fraction": round(result.obscured_fraction, 4),
    })

    if result.obscured_fraction > 0.05:
        bundle.gaps.append(
            f"{result.obscured_fraction * 100:.0f}% of the area was obscured by "
            "cloud in one or both scenes and was excluded from the comparison; "
            "change there is neither confirmed nor ruled out")
    gap_days = (after.acquired_on - before.acquired_on).days
    if gap_days > 20:
        bundle.gaps.append(
            f"{gap_days} days separate the two scenes, so the change is located "
            "in time only to that window; anything that appeared and was "
            "removed inside it is invisible")
    if abs(result.registration_shift[0]) + abs(result.registration_shift[1]) >= 3:
        bundle.gaps.append(
            f"the scenes needed a {result.registration_shift} cell alignment "
            "correction; residual sub-pixel misregistration remains a possible "
            "contributor at object edges")
    if baselines:
        for metric, b in sorted(baselines.items()):
            bundle.add("baseline", metric, b.to_dict() if hasattr(b, "to_dict") else b,
                       f"site history for {metric}")
    return bundle


def for_anomaly(finding: AnomalyFinding, aoi: Aoi, assessment,
                scenes: list[Scene], baselines: dict) -> EvidenceBundle:
    bundle = EvidenceBundle(
        id="ev-" + digest(finding.id, aoi.fingerprint)[:16],
        aoi_id=aoi.id, finding_id=finding.id, finding_kind="anomaly",
        created_at=datetime.now(timezone.utc), finding_at=finding.observed_at,
        narrative="; ".join(finding.reasons),
    )
    for s in scenes:
        bundle.add("scene", s.id, _scene_payload(s),
                   f"{s.constellation.value} acquisition used for today's counts")
    for metric, b in sorted(baselines.items()):
        bundle.add("baseline", metric, b.to_dict(),
                   f"median {b.overall.median:,.1f} over {b.overall.n} prior "
                   f"observations"
                   + (", with a weekday term" if b.has_weekday_term else ""))
    bundle.add("model", "ts-anomaly", {"version": "ts-anomaly-1.1.0"},
               "anomaly engine version")

    bundle.measurements.update({
        "score": finding.score,
        "confidence": finding.confidence,
        "baseline_observations": finding.baseline_n,
        **{f"deviation_{k}": round(v, 2)
           for k, v in sorted(assessment.deviations.items())},
        #: Slopes as well as today's values. A finding driven by a build-up no
        #: single day made unusual has to be able to show the build-up.
        **{f"trend_per_day_{k}": round(v, 4)
           for k, v in sorted(assessment.trends.items())},
    })
    for metric, why in sorted(assessment.skipped.items()):
        bundle.gaps.append(f"{metric.replace('_', ' ')} not assessed: {why}")
    if finding.baseline_n < 12:
        bundle.gaps.append(
            f"the comparison rests on {finding.baseline_n} prior observations, "
            "which is too few to characterise this site's normal range")
    #: The line that keeps the product honest, written into every anomaly
    #: bundle rather than left to the interface to remember.
    bundle.gaps.append(
        "this is a statistical deviation from the site's own record. It carries "
        "no assessment of cause or intent, and none should be inferred from the "
        "score")
    return bundle


# ---------------------------------------------------------------------------
# Packs
# ---------------------------------------------------------------------------

def pack(bundles: list[EvidenceBundle], aoi: Aoi,
         period: tuple[date, date]) -> dict:
    """A set of bundles with a manifest, for handing to somebody else.

    The manifest hash covers the bundle hashes, so a pack that has been edited
    after export does not verify -- which is the property that makes it worth
    attaching to a report that leaves the building.
    """
    entries = [b.to_dict() for b in bundles]
    return {
        "aoi": {"id": aoi.id, "name": aoi.name, "kind": aoi.kind.value,
                "fingerprint": aoi.fingerprint,
                "area_km2": round(aoi.area_km2, 2)},
        "period": {"start": period[0].isoformat(), "end": period[1].isoformat()},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bundle_count": len(entries),
        "bundles": entries,
        "manifest_sha256": digest([e["sha256"] for e in entries]),
    }


def verify(pack_doc: dict) -> tuple[bool, list[str]]:
    """Re-derive every hash in a pack. Returns (ok, problems)."""
    problems: list[str] = []
    hashes = []
    for entry in pack_doc.get("bundles", []):
        recomputed = digest(
            entry["aoi_id"], entry["finding_id"], entry["finding_kind"],
            entry["artefacts"],
            sorted(entry["measurements"].items(), key=lambda kv: kv[0]),
            sorted(entry["gaps"]),
        )
        if recomputed != entry["sha256"]:
            problems.append(
                f"bundle {entry['id']} does not match its recorded hash; its "
                "contents have changed since it was produced")
        hashes.append(entry["sha256"])
    if digest(hashes) != pack_doc.get("manifest_sha256"):
        problems.append(
            "the manifest hash does not match the bundles present; a bundle has "
            "been added, removed or reordered")
    return not problems, problems
