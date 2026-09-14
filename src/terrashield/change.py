"""Change detection: what is different between two scenes, and how sure we are.

This is the MVP (PRD section 41), and almost all of the difficulty is in the
three steps before the subtraction.

**Co-registration.** Two Sentinel-2 passes are not perfectly aligned. A
one-pixel shift puts a bright edge on one side of every building in the AOI and
a dark edge on the other, and a naive difference reports the entire built-up
area as changed. Integer-shift search costs a few hundred milliseconds and
removes the single largest source of false change.

**Radiometric normalisation.** The same site in June and December differs in
brightness by more than most construction does, because the sun is in a
different place. Matching the two scenes on robust percentiles -- the
pseudo-invariant-feature approach -- removes the illumination term without
removing the change, because a real change occupies a small fraction of the
AOI and therefore barely moves a percentile.

**A noise floor that is not contaminated by the signal.** Standard deviation
over a difference image is dominated by the changes you are looking for, so
thresholding at a multiple of it hides exactly the large events that matter.
Median absolute deviation is not, which is why it is used everywhere here.

Only then: threshold, open, label, classify, and hand back polygons that carry
their own evidence. Nothing is reported as a change unless the pair it came
from was co-registerable, cloud-free at that location, and from the same
constellation -- and when those conditions fail, the failure is returned as a
`ChangeResult` with a reason rather than as an empty list, because "no change"
and "could not look" are completely different statements to put in front of an
analyst.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .domain import (
    Aoi, ChangeEvent, ChangeType, Scene, Sensor, Severity,
)
from .geo import frame_for
from .raster import (
    Component, Mask, Raster, label_components, percentile, threshold,
)
from .sensors import cloud_mask
from .world import seed_of

MODEL_VERSION = "ts-change-1.3.0"

#: Below this a component is a handful of cells and could be anything.
#: 900 m2 is one small building at Sentinel-2 resolution.
MIN_CHANGE_AREA_M2 = 900.0

#: Largest share of an AOI a change can occupy and still be read as objects on
#: the water rather than as the water itself moving.
OBJECT_AREA_FRACTION = 0.02

#: Changes that are an object moving rather than the ground changing. Routed to
#: `ChangeResult.movements` and counted, never filed as change events.
MOVEMENT_TYPES = frozenset({
    ChangeType.OBJECT_APPEARED, ChangeType.OBJECT_DEPARTED,
})

#: Detection threshold, in robust standard deviations of the difference image.
#: Four and a half puts the expected false-positive count over a 400 x 300 grid
#: in single digits before morphological opening, and opening removes nearly
#: all of those because noise does not survive an erode-dilate pass.
SIGMA = 4.5


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class ChangeResult:
    """Everything one comparison produced, including the reason it produced nothing."""

    aoi_id: str
    before_scene_id: str
    after_scene_id: str
    events: list[ChangeEvent] = field(default_factory=list)
    #: Objects that arrived or left between the two scenes -- ships at a berth,
    #: aircraft on an apron. Kept separate from `events` and deliberately not
    #: written to the change record: a container terminal generates forty of
    #: these a fortnight, and storing each one individually buries the two
    #: warehouses that actually went up. They are activity, and activity is
    #: already represented properly as a counted metric with a baseline, so
    #: filing them again as change events is a second, worse copy of the same
    #: fact.
    movements: list[ChangeEvent] = field(default_factory=list)
    mask: Mask | None = None
    difference: Raster | None = None
    registration_shift: tuple[int, int] = (0, 0)
    registration_quality: float = 0.0
    noise_floor: float = 0.0
    gain: float = 1.0
    offset: float = 0.0
    obscured_fraction: float = 0.0
    usable: bool = True
    refusal: str = ""
    model_version: str = MODEL_VERSION

    @property
    def changed_area_m2(self) -> float:
        return sum(e.area_m2 for e in self.events)

    @property
    def arrivals(self) -> int:
        return sum(1 for e in self.movements
                   if e.change_type is ChangeType.OBJECT_APPEARED)

    @property
    def departures(self) -> int:
        return sum(1 for e in self.movements
                   if e.change_type is ChangeType.OBJECT_DEPARTED)

    def to_dict(self) -> dict:
        return {
            "aoi_id": self.aoi_id,
            "before_scene_id": self.before_scene_id,
            "after_scene_id": self.after_scene_id,
            "events": len(self.events),
            "movements": len(self.movements),
            "arrivals": self.arrivals,
            "departures": self.departures,
            "changed_area_m2": round(self.changed_area_m2, 1),
            "registration_shift_cells": list(self.registration_shift),
            "registration_quality": round(self.registration_quality, 4),
            "noise_floor": round(self.noise_floor, 5),
            "radiometric_gain": round(self.gain, 4),
            "radiometric_offset": round(self.offset, 5),
            "obscured_fraction": round(self.obscured_fraction, 4),
            "usable": self.usable,
            "refusal": self.refusal,
            "model_version": self.model_version,
        }


def _refused(aoi_id: str, before: Scene, after: Scene, reason: str) -> ChangeResult:
    return ChangeResult(aoi_id=aoi_id, before_scene_id=before.id,
                        after_scene_id=after.id, usable=False, refusal=reason)


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------

def pair_problems(before: Scene, after: Scene) -> str:
    """Why this pair cannot be differenced, or an empty string if it can."""
    if before.constellation is not after.constellation:
        return (f"{before.constellation.value} and {after.constellation.value} "
                "have different ground sample distances and spectral responses; "
                "differencing across constellations turns every field edge into "
                "a change")
    if not before.usable:
        return f"before scene unusable: {before.unusable_reason}"
    if not after.usable:
        return f"after scene unusable: {after.unusable_reason}"
    if after.acquired_at <= before.acquired_at:
        return "the after scene is not later than the before scene"
    if (before.sensor is Sensor.SAR
            and before.orbit and after.orbit and before.orbit != after.orbit):
        return (f"SAR scenes from different orbit tracks ({before.orbit} and "
                f"{after.orbit}) view the terrain from different geometries; "
                "their difference is dominated by that, not by change")
    return ""


# ---------------------------------------------------------------------------
# Co-registration and normalisation
# ---------------------------------------------------------------------------

#: How much better than no alignment at all the best shift has to score before
#: it is worth applying, as a fraction of the unshifted error.
#:
#: A shift is not a free guess. Moving a scene by one cell throws every sharp
#: edge in it one cell out of register, and a mis-registered edge is exactly
#: what the change detector is built to notice: it reports a bright rim along
#: one side of every static building and a dark rim along the other. So the
#: null hypothesis is "already aligned", and displacing a scene needs evidence.
#:
#: Measured over 81 same-sensor, same-track pairs across the four demonstration
#: sites, March-May 2026, against the geolocation error the sensor model
#: actually injected:
#:
#:                                   exact   within 1   worse than no shift
#:     no mask, no guard             64/81      79/81                  3/81
#:     cloud-masked only             70/81      80/81                  2/81
#:     cloud-masked + this guard     62/81      78/81                  0/81
#:
#: Two percent is where it is because pairs that were already aligned -- true
#: offset under half a cell -- score at most 0.0144, and this has to sit above
#: that ceiling to suppress them. A one percent threshold keeps eight more
#: exact matches and lets one spurious shift back through, which is the wrong
#: side of the trade: the eight are SAR pairs left with a one-cell residual,
#: already softened by the 3x3 multi-look, while the one displaces a correctly
#: aligned scene and invents a rim along every static edge in it.
#:
#: Genuine shifts score up to 0.40 with a median of 0.09, so the guard is not
#: close to them.
MIN_REGISTRATION_QUALITY = 0.02


def coregister(before: Raster, after: Raster, max_shift: int = 3,
               exclude: Mask | None = None) -> tuple[int, int, float]:
    """Find the integer cell shift that best aligns `after` onto `before`.

    Scored on mean absolute difference over a decimated sample of cells --
    every third cell in each direction, which is a ninth of the work and
    statistically indistinguishable at this grid size.

    `exclude` must cover everything obscured in *either* scene, and passing it
    is not optional in practice. Cloud is bright, it sits in one scene and not
    the other, and it does not move with the ground. Leaving it in the score
    adds a large, nearly shift-invariant constant to every candidate, which
    flattens the curve until the minimum is decided by noise. On a Landsat pair
    over Sardar Sarovar with 6% cloud before and 29% after, the scores across
    seven candidate row shifts spanned 0.12108 to 0.12186 -- a 0.06% spread --
    and the winner was two rows from the truth. Masking the cloud out turns the
    same curve into a clean minimum with a 7% margin.

    A cell is skipped if it is obscured at either end of the comparison, so the
    same ground is scored in both scenes whatever the shift.
    """
    step = 3
    rows = range(max_shift, before.height - max_shift, step)
    cols = range(max_shift, before.width - max_shift, step)

    def error(d_col: int, d_row: int) -> tuple[float, int]:
        total = 0.0
        n = 0
        for row in rows:
            for col in cols:
                if exclude is not None and (exclude.get(col, row)
                                            or exclude.get(col + d_col,
                                                           row + d_row)):
                    continue
                total += abs(before.get(col, row)
                             - after.get(col + d_col, row + d_row))
                n += 1
        return total / max(n, 1), n

    zero, seen = error(0, 0)
    #: With little clear ground left there is nothing to register against, and
    #: a shift fitted to a handful of cells is worse than no shift at all.
    if seen < 64 or zero <= 1e-9:
        return 0, 0, 0.0

    best: tuple[int, int, float] | None = None
    for d_row in range(-max_shift, max_shift + 1):
        for d_col in range(-max_shift, max_shift + 1):
            score, n = error(d_col, d_row)
            #: A candidate that clears far less ground than the unshifted one
            #: is scoring different terrain, not a better alignment.
            if n < seen // 2:
                continue
            if best is None or score < best[2]:
                best = (d_col, d_row, score)
    assert best is not None
    d_col, d_row, score = best

    #: Quality is how much better the best alignment is than no alignment at
    #: all. Near zero means the shift search found nothing to improve, which is
    #: normal for an already-aligned pair -- so believe that rather than
    #: applying the argmin of a flat surface.
    quality = (zero - score) / zero
    if quality < MIN_REGISTRATION_QUALITY:
        return 0, 0, quality
    return d_col, d_row, quality


def normalise(after: Raster, before: Raster,
              exclude: Mask | None = None) -> tuple[float, float]:
    """Gain and offset mapping `after` onto `before`'s radiometry.

    Fitted on the 20th and 80th percentiles rather than on a least-squares line,
    because a least-squares fit is pulled by the changed area -- the very thing
    the normalisation is supposed to leave alone.
    """
    a_cells, b_cells = after.cells, before.cells
    if exclude is not None:
        a_cells = [v for v, m in zip(a_cells, exclude.bits) if not m]
        b_cells = [v for v, m in zip(b_cells, exclude.bits) if not m]
    if len(a_cells) < 32:
        return 1.0, 0.0
    a_lo, a_hi = percentile(a_cells, 20.0), percentile(a_cells, 80.0)
    b_lo, b_hi = percentile(b_cells, 20.0), percentile(b_cells, 80.0)
    spread = a_hi - a_lo
    if abs(spread) < 1e-6:
        return 1.0, b_lo - a_lo
    gain = (b_hi - b_lo) / spread
    #: A gain far from 1 means the two scenes are not comparable at all -- a
    #: different product level, or one of them mostly cloud. Clamp and let the
    #: noise floor do the rest rather than manufacturing a fit.
    gain = min(2.0, max(0.5, gain))
    return gain, b_lo - gain * a_lo


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def compare(aoi: Aoi, before_scene: Scene, after_scene: Scene,
            before: Raster, after: Raster,
            before_cloud: Mask | None = None,
            after_cloud: Mask | None = None,
            before_water: Mask | None = None,
            after_water: Mask | None = None,
            sigma: float = SIGMA,
            min_area_m2: float = MIN_CHANGE_AREA_M2) -> ChangeResult:
    """Compare two scenes over one AOI."""
    problem = pair_problems(before_scene, after_scene)
    if problem:
        return _refused(aoi.id, before_scene, after_scene, problem)
    if (before.width, before.height) != (after.width, after.height):
        return _refused(aoi.id, before_scene, after_scene,
                        "scenes are on different grids; resample to a common "
                        "ground sample distance first")

    b_cloud = cloud_mask(before, before_scene) if before_cloud is None else before_cloud
    a_cloud = cloud_mask(after, after_scene) if after_cloud is None else after_cloud
    obscured = Mask.like(before)
    obscured.bits = [x or y for x, y in zip(b_cloud.bits, a_cloud.bits)]
    if obscured.fraction > 0.60:
        return _refused(
            aoi.id, before_scene, after_scene,
            f"{obscured.fraction * 100:.0f}% of the AOI is obscured in one or "
            "both scenes; the visible remainder is too small to support a "
            "change statement over the area")

    #: Spatial multi-looking before anything else. Speckle in a single-look
    #: SAR image is severe enough that a plain difference is mostly noise, and
    #: averaging a 3x3 window trades a factor of three in resolution for a
    #: factor of three in noise -- which is the right trade when the objects of
    #: interest are buildings rather than vehicles. Every SAR processor does
    #: this; skipping it is how SAR gets a reputation for being unusable.
    if before_scene.sensor is Sensor.SAR:
        before = before.box_blur(1)
        after = after.box_blur(1)

    d_col, d_row, quality = coregister(before, after, exclude=obscured)
    aligned = after.shifted(-d_col, -d_row)

    gain, offset = normalise(aligned, before, exclude=obscured)
    aligned = aligned.scaled(gain, offset)

    diff = (log_ratio(aligned, before) if before_scene.sensor is Sensor.SAR
            else aligned.difference(before))
    #: The noise floor is estimated off-cloud, because a cloud edge is a real
    #: radiometric difference and including it inflates the floor enough to
    #: hide genuine change elsewhere in the scene.
    clear = [v for v, m in zip(diff.cells, obscured.bits) if not m]
    noise = 1.4826 * _mad(clear) if clear else 1.4826 * diff.mad()
    level = max(sigma * noise, 0.02)

    m = threshold(diff, level, absolute=True)
    for i, ob in enumerate(obscured.bits):
        if ob:
            m.bits[i] = False
    m = m.opened(1)

    result = ChangeResult(
        aoi_id=aoi.id, before_scene_id=before_scene.id,
        after_scene_id=after_scene.id, mask=m, difference=diff,
        registration_shift=(d_col, d_row), registration_quality=round(quality, 4),
        noise_floor=noise, gain=gain, offset=offset,
        obscured_fraction=round(obscured.fraction, 4),
    )

    frame = frame_for(aoi.boundary)
    min_cells = max(2, int(round(min_area_m2 / (diff.gsd_m ** 2))))
    b_tex = before.texture(2)
    a_tex = aligned.texture(2)
    for comp in label_components(m, min_cells=min_cells):
        event = _describe(
            aoi, before_scene, after_scene, comp, diff, before, aligned,
            b_tex, a_tex, noise, frame, before_water, after_water)
        if event.change_type in MOVEMENT_TYPES:
            result.movements.append(event)
        else:
            result.events.append(event)
    result.events.sort(key=lambda e: (-e.severity.rank, -e.area_m2))
    result.movements.sort(key=lambda e: -e.area_m2)
    return result


def log_ratio(after: Raster, before: Raster, floor: float = 0.01) -> Raster:
    """log(after / before). The right difference operator for SAR.

    Speckle is multiplicative, not additive: a bright cell and a dark cell in
    the same homogeneous field carry noise proportional to their own value. A
    subtraction therefore has a noise floor that varies across the image, and a
    single global threshold is simultaneously too tight over water and too loose
    over a metal yard. Taking the logarithm converts the multiplicative noise to
    additive noise with constant variance, which is exactly what a single
    threshold assumes.

    The output is in natural-log units; a value of 0.69 is a doubling of
    backscatter, and roughly 3 dB.
    """
    before._require_same_grid(after)
    out = before.like(band="log-ratio")
    out.cells = [math.log(max(a, floor) / max(b, floor))
                 for a, b in zip(after.cells, before.cells)]
    return out


def _mad(values: list[float]) -> float:
    med = percentile(values, 50.0)
    return percentile([abs(v - med) for v in values], 50.0)


def _mean_over(r: Raster, comp: Component,
               cells: list[tuple[int, int]] | None = None) -> float:
    pts = cells if cells is not None else comp.cells
    return sum(r.get(c, row) for c, row in pts) / max(len(pts), 1)


def interior(comp: Component, margin: int = 2) -> list[tuple[int, int]]:
    """The component minus a border `margin` cells wide.

    Texture has to be measured here, not over the whole component. A solar
    block is uniform inside and has a hard step at its edge; the texture filter
    smears that step a couple of cells inward, so a mean over every cell in the
    block is dominated by its own perimeter and reports local variation rising
    when in fact it collapsed. That single mistake relabels "a new structure
    appeared" as "someone disturbed the ground", which is a materially
    different thing to put in front of an analyst.
    """
    #: Grown inward from the rim rather than testing each cell against its own
    #: 81-cell neighbourhood: the same answer, and it does not turn a large
    #: region into millions of set lookups.
    own = {(c, r) for c, r in comp.cells}
    near: set[tuple[int, int]] = set()
    for bc, br in comp.boundary_cells:
        for dc in range(-margin, margin + 1):
            for dr in range(-margin, margin + 1):
                p = (bc + dc, br + dr)
                if p not in own:
                    #: A component cell is non-interior when something outside
                    #: sits within `margin` of it, so mark the cells around
                    #: each outside cell rather than around the boundary.
                    for ec in range(-margin, margin + 1):
                        for er in range(-margin, margin + 1):
                            near.add((p[0] + ec, p[1] + er))
    return [(c, r) for c, r in comp.cells if (c, r) not in near]


def _describe(aoi: Aoi, before_scene: Scene, after_scene: Scene,
              comp: Component, diff: Raster, before: Raster, after: Raster,
              b_tex: Raster, a_tex: Raster, noise: float, frame,
              before_water: Mask | None, after_water: Mask | None) -> ChangeEvent:
    signed = _mean_over(diff, comp)
    magnitude = abs(signed)
    area = comp.area_m2
    major, minor, _ = comp.axes
    elong = comp.elongation
    fill = comp.fill
    #: Four cells, not two. The texture filter is a blur of a blur, each of
    #: radius two, so an edge influences cells up to four away; eroding by less
    #: leaves the component's own boundary step inside the region whose texture
    #: is being measured.
    core = interior(comp, 4) or interior(comp, 2) or comp.cells
    tex_before = _mean_over(b_tex, comp, core)
    tex_after = _mean_over(a_tex, comp, core)
    tex_ratio = tex_after / max(tex_before, 1e-6)
    before_level = _mean_over(before, comp, core)
    after_level = _mean_over(after, comp, core)

    was_water = _fraction_true(before_water, core)
    is_water = _fraction_true(after_water, core)
    surround = _ring(comp, diff.width, diff.height)
    afloat = max(_fraction_true(before_water, surround),
                 _fraction_true(after_water, surround))
    kind, explanation = _classify(
        area, elong, fill, signed, magnitude, tex_ratio,
        before_level, after_level, was_water, is_water, afloat,
        after_scene.sensor, aoi.area_km2 * 1_000_000)

    #: Confidence from how far above the noise floor the change sits and how
    #: much of it there is. Area matters because a 4.5-sigma excursion over
    #: nine cells happens by chance somewhere in a 120,000-cell grid, and over
    #: nine hundred it does not.
    snr = magnitude / max(noise, 1e-9)
    snr_term = min(1.0, max(0.0, (snr - 3.0) / 6.0))
    area_term = min(1.0, (area / (8 * MIN_CHANGE_AREA_M2)) ** 0.5)
    confidence = round(min(0.97, 0.25 + 0.55 * snr_term + 0.20 * area_term), 3)

    severity = _severity(kind, area, confidence, aoi)

    c0, r0, c1, r1 = comp.bbox_cells
    gsd = diff.gsd_m
    ring = frame.ring_to_lonlat([
        (diff.origin_e + c0 * gsd, diff.origin_n - (r1 + 1) * gsd),
        (diff.origin_e + (c1 + 1) * gsd, diff.origin_n - (r1 + 1) * gsd),
        (diff.origin_e + (c1 + 1) * gsd, diff.origin_n - r0 * gsd),
        (diff.origin_e + c0 * gsd, diff.origin_n - r0 * gsd),
    ])
    ce, cr = comp.centroid_cell
    ev_id = "chg-" + format(
        seed_of(before_scene.id, after_scene.id, round(ce, 1), round(cr, 1))
        & 0xFFFFFFFFFFFF, "012x")

    return ChangeEvent(
        id=ev_id, aoi_id=aoi.id, change_type=kind,
        detected_at=after_scene.acquired_at,
        before_scene_id=before_scene.id, after_scene_id=after_scene.id,
        geometry=ring, area_m2=round(area, 1), confidence=confidence,
        magnitude=round(magnitude, 4), severity=severity,
        explanation=explanation, model_version=MODEL_VERSION,
    )


def _ring(comp: Component, width: int, height: int,
          inner: int = 2, outer: int = 5) -> list[tuple[int, int]]:
    """The annulus around a component, grown from its rim."""
    own = {(c, r) for c, r in comp.cells}
    out: set[tuple[int, int]] = set()
    for bc, br in comp.boundary_cells:
        for dc in range(-outer, outer + 1):
            for dr in range(-outer, outer + 1):
                p = (bc + dc, br + dr)
                if p in own or not (0 <= p[0] < width and 0 <= p[1] < height):
                    continue
                if inner > 1 and any(
                        (p[0] + ec, p[1] + er) in own
                        for ec in range(-(inner - 1), inner)
                        for er in range(-(inner - 1), inner)):
                    continue
                out.add(p)
    return sorted(out)


def _fraction_true(mask: Mask | None, cells: list[tuple[int, int]]) -> float:
    """How much of a region a quality band marks. -1 when the band is absent."""
    if mask is None or not cells:
        return -1.0
    return sum(1 for c, r in cells if mask.get(c, r)) / len(cells)


def _classify(area: float, elong: float, fill: float, signed: float,
              magnitude: float, tex_ratio: float, before_level: float,
              after_level: float, was_water: float, is_water: float,
              afloat: float, sensor: Sensor,
              aoi_area_m2: float) -> tuple[ChangeType, str]:
    """Name the change, and say in one sentence what the measurement was.

    The brightness *direction* deliberately does not decide between "something
    was built" and "something was removed". A new photovoltaic block is much
    darker than the desert it replaced; a demolished warehouse is usually
    brighter than the roof that was there. Direction tracks materials, not
    construction.

    Texture does track construction -- but in opposite directions per sensor,
    which is why this function has to know which one it is looking at.

    In optical, building something replaces a varied natural surface with a
    uniform engineered one and local variation falls. In SAR the same event
    does the reverse: a vertical wall produces a bright layover line in front
    of it and a radar shadow behind, so a new building is a sharp *increase* in
    local variation against flat ground. Applying the optical rule to a SAR
    pair labels every new structure "disturbed ground", which is how the two
    buildings that went up in a border sector during the monsoon -- the only
    window where SAR is the sole usable sensor -- get filed as earthworks.
    """
    sar = sensor is Sensor.SAR
    units = ("in backscatter (natural-log ratio)" if sar
             else "in reflectance")
    direction = "brighter" if signed > 0 else "darker"

    if elong >= 6.0 and area >= 1200:
        return (ChangeType.LINEAR_FEATURE,
                f"linear feature {major_m(area, elong):.0f} m long and about "
                f"{minor_m(area, elong):.0f} m wide, {direction} than before; "
                "consistent with a new track, road or berm")

    #: Water comes from the provider's scene classification band, never from
    #: brightness. A photovoltaic array is as dark as a reservoir in the
    #: visible bands and completely unlike one in the near infrared, and the
    #: first version of this function called a new solar block "11.7 hectares
    #: of inundation" for exactly that reason. When the band is absent (-1),
    #: no water call is made at all rather than a guess from brightness.
    became_water = was_water >= 0 and was_water < 0.3 and is_water > 0.6
    ceased_water = was_water > 0.6 and 0 <= is_water < 0.3

    #: A change fully ringed by water is a thing floating on it, not a moved
    #: shoreline. Inundation and drawdown move the land/water boundary, so
    #: their footprint always touches land somewhere; a berthed ship does not.
    #: Without this test every vessel arrival at a container terminal is
    #: reported as a hectare of flooding, which is how a maritime feed becomes
    #: unreadable in its first week.
    #:
    #: Scale relative to the AOI, not absolute size. A fixed six-hectare cap
    #: was the first attempt and it failed both ways: berthed vessels cluster,
    #: so a row of them leaving during a congestion episode merges into one
    #: thirteen-hectare region that is still ringed by water, and the cap sent
    #: exactly those to the inundation branch -- the port's feed carried "13.6
    #: ha changed from a land signature to a water signature", at high
    #: severity, four times in a fortnight.
    #:
    #: Removing the cap entirely failed the other way: a reservoir drawdown is
    #: a long strip that touches land along one short side and is surrounded by
    #: water everywhere else, so the perimeter test alone called 1.8 km2 of
    #: exposed bank a departing object.
    #:
    #: Objects on water cannot be a substantial fraction of a monitored area.
    #: A change in water extent can be, and usually is. Two percent separates
    #: the two cases here by more than an order of magnitude in both
    #: directions, which is the kind of margin a threshold needs to survive
    #: contact with a different estate.
    if afloat > 0.75 and area < OBJECT_AREA_FRACTION * aoi_area_m2:
        plural = (" The footprint is larger than a single vessel, so this is "
                  "probably several moving together." if area > 40_000 else "")
        if became_water or signed < 0:
            return (ChangeType.OBJECT_DEPARTED,
                    f"a {major_m(area, elong):.0f} m object on the water in the "
                    "earlier scene is absent in the later one; routine vessel "
                    "movement unless the berth is under watch." + plural)
        return (ChangeType.OBJECT_APPEARED,
                f"a {major_m(area, elong):.0f} m object is present on the water "
                "in the later scene and absent in the earlier one." + plural)
    if became_water and area >= 5000:
        return (ChangeType.INUNDATION,
                f"{area / 10_000:.1f} ha changed from a land signature "
                f"({before_level:.2f}) to a water signature ({after_level:.2f})")
    if ceased_water and area >= 5000:
        return (ChangeType.SURFACE_CHANGE,
                f"{area / 10_000:.1f} ha of previously open water now returns a "
                f"land signature ({after_level:.2f}); consistent with a "
                "falling water level or exposed bank")


    compact = fill >= 0.55 and elong <= 4.0
    if compact and area >= 1500:
        if sar and tex_ratio >= 1.6:
            return (ChangeType.NEW_STRUCTURE,
                    f"compact {area:,.0f} m2 footprint with backscatter "
                    f"structure up {(tex_ratio - 1) * 100:.0f}%; bright layover "
                    "and shadow against flat ground is the radar signature of a "
                    "vertical structure")
        if not sar and tex_ratio <= 0.80:
            return (ChangeType.NEW_STRUCTURE,
                    f"compact {area:,.0f} m2 footprint, {direction} than before, "
                    f"with local variation down {(1 - tex_ratio) * 100:.0f}% -- "
                    "a uniform engineered surface has replaced a varied natural "
                    "one")
        if not sar and tex_ratio >= 1.30:
            return (ChangeType.CONSTRUCTION_ACTIVITY,
                    f"compact {area:,.0f} m2 area with local variation up "
                    f"{(tex_ratio - 1) * 100:.0f}%; consistent with disturbed "
                    "ground, earthworks or materials laid out")
        if sar and tex_ratio <= 0.70:
            return (ChangeType.STRUCTURE_REMOVED,
                    f"compact {area:,.0f} m2 footprint whose layover and shadow "
                    f"returns have fallen {(1 - tex_ratio) * 100:.0f}%; "
                    "consistent with a vertical structure no longer present")
        return (ChangeType.NEW_STRUCTURE,
                f"compact {area:,.0f} m2 footprint {direction} than before, with "
                "little change in local variation; structure inferred from shape "
                "rather than texture, so confidence is capped accordingly")

    if area >= 50_000 and magnitude < 0.12:
        return (ChangeType.SURFACE_CHANGE,
                f"{area / 10_000:.1f} ha {direction} by {magnitude:.3f} "
                f"{units} with no change in shape; consistent with a "
                "seasonal or agricultural surface change rather than "
                "construction")

    if tex_ratio >= 1.6 and signed < 0:
        return (ChangeType.POSSIBLE_DAMAGE,
                f"{area:,.0f} m2 darker with local variation up "
                f"{(tex_ratio - 1) * 100:.0f}%; a previously uniform surface has "
                "become broken. Requires analyst review -- collapse, demolition "
                "and heavy vehicle disturbance are not separable at this "
                "resolution")

    return (ChangeType.SURFACE_CHANGE,
            f"{area:,.0f} m2 {direction} by {magnitude:.3f} {units}; "
            "shape and texture do not support a more specific description")


def major_m(area: float, elong: float) -> float:
    return (area * elong) ** 0.5


def minor_m(area: float, elong: float) -> float:
    return (area / elong) ** 0.5


#: Which change types matter more at which kind of site. A new structure inside
#: a port is routine; the same structure in a remote border sector is not, and a
#: platform that scores them identically buries the second under the first.
from .domain import AoiKind  # noqa: E402  (kept next to the table it feeds)

SITE_WEIGHTS: dict[AoiKind, dict[ChangeType, float]] = {
    AoiKind.BORDER_SECTOR: {
        ChangeType.NEW_STRUCTURE: 1.6, ChangeType.LINEAR_FEATURE: 1.5,
        ChangeType.CONSTRUCTION_ACTIVITY: 1.4,
    },
    AoiKind.PORT: {
        ChangeType.NEW_STRUCTURE: 0.9, ChangeType.SURFACE_CHANGE: 0.7,
    },
    AoiKind.ENERGY: {
        ChangeType.POSSIBLE_DAMAGE: 1.6, ChangeType.NEW_STRUCTURE: 1.0,
    },
    AoiKind.DAM: {
        ChangeType.INUNDATION: 1.5, ChangeType.POSSIBLE_DAMAGE: 1.8,
        #: Reservoir extent is the measurement a dam is monitored for, so a
        #: surface change here is the signal rather than the background it is
        #: at a port or on farmland.
        ChangeType.SURFACE_CHANGE: 2.0,
    },
}


def _severity(kind: ChangeType, area_m2: float, confidence: float,
              aoi: Aoi) -> Severity:
    base = {
        ChangeType.OBJECT_APPEARED: 0.8,
        ChangeType.OBJECT_DEPARTED: 0.6,
        ChangeType.NEW_STRUCTURE: 2.4,
        ChangeType.STRUCTURE_REMOVED: 2.4,
        ChangeType.POSSIBLE_DAMAGE: 3.0,
        ChangeType.CONSTRUCTION_ACTIVITY: 1.8,
        ChangeType.LINEAR_FEATURE: 2.0,
        ChangeType.INUNDATION: 2.6,
        ChangeType.SURFACE_CHANGE: 1.0,
    }[kind]
    weight = SITE_WEIGHTS.get(aoi.kind, {}).get(kind, 1.0)
    size = min(1.0, (area_m2 / 40_000) ** 0.5)
    score = base * weight * (0.55 + 0.45 * size) * (0.5 + 0.5 * confidence)
    if score >= 3.0:
        return Severity.CRITICAL
    if score >= 2.1:
        return Severity.HIGH
    if score >= 1.3:
        return Severity.MEDIUM
    return Severity.LOW
