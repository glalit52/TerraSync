"""Object detection: what is in one scene, and what the sensor could not have seen.

The second half of that sentence is the part most detection engines skip, and
it is the part that decides whether an intelligence product is trustworthy.

A 4.5 m car occupies 20% of one Sentinel-2 pixel. No model, however good,
recovers it -- the information is not in the data. A system that returns
"0 vehicles detected" from a 10 m scene has told the analyst something false by
implication: that it looked and found none. So `detect` returns a
`DetectionResult` carrying both the detections and the classes it refused to
look for, with the reason, and the dashboard shows the refusal next to the
counts. That refusal is also a sales conversation -- vehicle-level
pattern-of-life requires tasked sub-metre imagery, which has a price -- and it
is better had at the demo than in month three of a pilot.

The detector itself is a classical pipeline: background suppression, robust
thresholding, connected components, then classification against a table of real
object dimensions. A fine-tuned YOLO or a geospatial foundation model would beat
it on a real scene and should replace it behind the same interface. What should
not change is the shape of the output: every detection carries the scene it came
from, its measured extent, and a confidence that means something, because
`calibration_report` measures whether it does.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .domain import Aoi, Detection, ObjectClass, Scene, Sensor
from .geo import frame_for, haversine_m
from .raster import Component, Mask, Raster, label_components, percentile, threshold
from .sensors import cloud_mask
from .world import seed_of

MODEL_VERSION = "ts-detect-1.2.0"


# ---------------------------------------------------------------------------
# What each class looks like
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClassProfile:
    """The measurable signature of one object class.

    Dimensions are real -- a narrow-body airliner is 38 m, a Panamax ship 240 m,
    a shipping-container truck 16 m. `polarity` is whether the object is
    brighter (+1) or darker (-1) than its surroundings, and `on_water` restricts
    a class to the water mask recovered from the scene itself, which is what
    stops a bright roof being called a vessel.
    """

    object_class: ObjectClass
    min_extent_m: float
    max_extent_m: float
    min_elongation: float = 1.0
    max_elongation: float = 4.0
    polarity: int = 1
    min_fill: float = 0.40
    on_water: bool | None = None      # True: only on water. False: only on land.
    transient: bool = True

    def score(self, extent_m: float, elongation: float, fill: float,
              polarity: int, over_water: bool) -> float:
        """0..1 fit. Zero means this class is excluded, not merely unlikely."""
        if polarity != self.polarity:
            return 0.0
        if self.on_water is not None and self.on_water != over_water:
            return 0.0
        if not (self.min_extent_m <= extent_m <= self.max_extent_m):
            return 0.0
        if not (self.min_elongation <= elongation <= self.max_elongation):
            return 0.0
        if fill < self.min_fill:
            return 0.0
        #: Inside the box, prefer the middle of the expected size range and a
        #: solid shape. Both are weak preferences; the hard gates above do the
        #: real work, and pretending otherwise would inflate confidence.
        mid = (self.min_extent_m + self.max_extent_m) / 2
        span = max(self.max_extent_m - self.min_extent_m, 1e-6)
        size_fit = 1.0 - min(1.0, 2 * abs(extent_m - mid) / span)
        return 0.55 + 0.30 * size_fit + 0.15 * min(1.0, fill)


PROFILES: tuple[ClassProfile, ...] = (
    ClassProfile(ObjectClass.VESSEL, 90, 420, 2.2, 14.0, +1, 0.35, on_water=True),
    ClassProfile(ObjectClass.SMALL_BOAT, 8, 30, 1.6, 8.0, +1, 0.30, on_water=True),
    ClassProfile(ObjectClass.AIRCRAFT, 22, 80, 1.0, 1.8, +1, 0.45, on_water=False),
    ClassProfile(ObjectClass.HELICOPTER, 10, 24, 1.0, 1.8, +1, 0.45, on_water=False),
    ClassProfile(ObjectClass.BUILDING, 18, 340, 1.0, 4.5, +1, 0.55,
                 on_water=False, transient=False),
    ClassProfile(ObjectClass.STORAGE_TANK, 14, 100, 1.0, 1.25, +1, 0.62,
                 on_water=False, transient=False),
    ClassProfile(ObjectClass.CONTAINER_STACK, 14, 48, 1.2, 3.5, +1, 0.45),
    ClassProfile(ObjectClass.TRUCK, 9, 26, 2.5, 9.0, +1, 0.35, on_water=False),
    ClassProfile(ObjectClass.CONSTRUCTION_VEHICLE, 6, 16, 1.4, 4.0, +1, 0.35,
                 on_water=False),
    ClassProfile(ObjectClass.VEHICLE, 3, 8, 1.3, 4.0, +1, 0.30, on_water=False),
    ClassProfile(ObjectClass.TOWER, 4, 30, 1.0, 2.2, +1, 0.50,
                 on_water=False, transient=False),
    ClassProfile(ObjectClass.SOLAR_ARRAY, 120, 900, 1.0, 3.0, -1, 0.55,
                 on_water=False, transient=False),
    ClassProfile(ObjectClass.RUNWAY, 900, 5000, 12.0, 90.0, +1, 0.45,
                 on_water=False, transient=False),
    ClassProfile(ObjectClass.BRIDGE, 180, 4000, 4.5, 60.0, +1, 0.40,
                 transient=False),
    ClassProfile(ObjectClass.ROAD, 150, 6000, 8.0, 200.0, +1, 0.20,
                 on_water=False, transient=False),
)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class SkippedClass:
    """A class the sensor cannot support, and why. Shown to the analyst.

    `partial` distinguishes "this class is invisible at this resolution" from
    "the small end of this class is invisible". The second is still worth
    saying -- a count of buildings from 30 m Landsat is a count of large
    buildings -- but it does not make the count meaningless.
    """

    object_class: ObjectClass
    reason: str
    needs_gsd_m: float
    partial: bool = False


@dataclass
class DetectionResult:
    scene_id: str
    aoi_id: str
    detections: list[Detection] = field(default_factory=list)
    skipped: list[SkippedClass] = field(default_factory=list)
    obscured_fraction: float = 0.0
    model_version: str = MODEL_VERSION

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.detections:
            out[d.object_class.value] = out.get(d.object_class.value, 0) + 1
        return out

    def count_of(self, klass: ObjectClass) -> int:
        return sum(1 for d in self.detections if d.object_class is klass)

    def reportable(self, klass: ObjectClass) -> bool:
        """Whether a count of this class from this scene means anything at all."""
        return all(s.object_class is not klass or s.partial for s in self.skipped)

    def caveat(self, klass: ObjectClass) -> str:
        """The resolution caveat to show beside a count, if there is one."""
        for s in self.skipped:
            if s.object_class is klass:
                return s.reason
        return ""


# ---------------------------------------------------------------------------
# Scene context
# ---------------------------------------------------------------------------

def water_mask(r: Raster, sensor: Sensor) -> Mask:
    """Where the scene is water, recovered from the pixels.

    Dark and smooth in optical; very dark in SAR, where a calm surface reflects
    away from the sensor and returns almost nothing. Recovering it rather than
    reading it from a land-cover layer matters because the thing that most often
    changes at a port or a dam *is* the water boundary.
    """
    smooth = r.box_blur(2)
    tex = r.texture(2)
    m = Mask.like(r)
    if sensor is Sensor.SAR:
        cut = max(0.10, percentile(r.cells, 12.0))
        m.bits = [v < cut for v in smooth.cells]
    else:
        cut = max(0.10, percentile(r.cells, 15.0))
        m.bits = [v < cut and t < 0.020 for v, t in zip(smooth.cells, tex.cells)]
    return m.opened(1).closed(1)


def _skips(gsd_m: float, sensor: Sensor) -> list[SkippedClass]:
    limit = 2.5 * gsd_m
    out: list[SkippedClass] = []
    for p in PROFILES:
        if p.max_extent_m < limit:
            out.append(SkippedClass(
                p.object_class,
                f"{p.object_class.value} is at most {p.max_extent_m:.0f} m across; "
                f"at {gsd_m:.1f} m ground sample distance the smallest reliably "
                f"resolvable object is {limit:.1f} m",
                needs_gsd_m=round(p.max_extent_m / 2.5, 2)))
        elif p.min_extent_m < limit <= p.max_extent_m and p.min_extent_m < limit * 0.8:
            out.append(SkippedClass(
                p.object_class,
                f"only {p.object_class.value} larger than {limit:.1f} m can be "
                f"resolved at {gsd_m:.1f} m; smaller examples are counted as "
                "absent, not as zero",
                needs_gsd_m=round(p.min_extent_m / 2.5, 2), partial=True))
    if sensor is Sensor.SAR:
        out.append(SkippedClass(
            ObjectClass.SOLAR_ARRAY,
            "photovoltaic arrays are defined here by low optical reflectance; "
            "SAR sees their mounting structure instead and the class does not "
            "transfer", needs_gsd_m=10.0))
    return out


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

#: Background window radii, in metres, plus a global pass.
#:
#: Local contrast only finds objects *smaller* than the window it is measured
#: against: the middle of a 500 m solar block is identical to its own local
#: mean, so a single 60 m window sees the block's edges and nothing else. One
#: window is therefore one object-size band, and a detector with one window is
#: a detector for one size of thing. These four bands span a car to a solar
#: farm; `None` is a whole-scene background, which is what recovers large
#: uniform regions.
SCALES_M: tuple[float | None, ...] = (60.0, 200.0, 700.0, None)


#: Chosen from a sweep against the synthetic estate, not from taste. At 0.35
#: the lowest confidence bin is right 6% of the time; raising the gate to 0.50
#: takes precision from 0.69 to 0.85 and false positives from 0.14 to 0.055 per
#: square kilometre per look, while recall falls only from 0.82 to 0.81 --
#: everything discarded was noise. Re-run evaluate.score_scene on a customer's
#: labelled scenes before trusting this number on their imagery.
DEFAULT_MIN_CONFIDENCE = 0.50


def detect(aoi: Aoi, scene: Scene, r: Raster,
           min_confidence: float = DEFAULT_MIN_CONFIDENCE,
           obscured: Mask | None = None,
           water: Mask | None = None) -> DetectionResult:
    """Find objects in one rendered scene, across every object-size band.

    `obscured` is the provider's cloud mask. Pass it. Cells under cloud are
    excluded from every scale rather than analysed and later explained away,
    and the fraction excluded is reported so that a count taken through a gap
    in the cloud is never mistaken for a count over the whole AOI.
    """
    gsd = r.gsd_m
    result = DetectionResult(scene_id=scene.id, aoi_id=aoi.id,
                             skipped=_skips(gsd, scene.sensor))
    frame = frame_for(aoi.boundary)

    obscured = cloud_mask(r, scene) if obscured is None else obscured
    result.obscured_fraction = round(obscured.fraction, 4)
    #: Prefer the provider's water band; fall back to the brightness estimate
    #: only when there is none, and note that the fallback confuses dark
    #: engineered surfaces with water.
    water = water_mask(r, scene.sensor) if water is None else water

    candidates: list[Detection] = []
    for scale_m in SCALES_M:
        if scale_m is not None and scale_m < 2.5 * gsd:
            continue
        contrast = _contrast_at(r, scale_m)
        noise = 1.4826 * contrast.mad()
        level = max(3.0 * noise, 0.035)
        for polarity in (+1, -1):
            signed = contrast if polarity > 0 else contrast.scaled(-1.0)
            m = threshold(signed, level, absolute=False)
            for i, ob in enumerate(obscured.bits):
                if ob:
                    m.bits[i] = False
            if gsd <= 2.0:
                m = m.opened(1)
            for comp in label_components(m, min_cells=2):
                det = _classify(aoi, scene, r, contrast, comp, polarity, water,
                                frame, noise, min_confidence)
                if det is not None:
                    candidates.append(det)

    result.detections = apply_context_rules(suppress_overlaps(candidates))
    result.detections.sort(key=lambda d: (-d.confidence, d.object_class.value))
    return result


def _contrast_at(r: Raster, scale_m: float | None) -> Raster:
    """The scene minus its background at one spatial scale."""
    if scale_m is None:
        level = r.median()
        out = r.like(band="contrast")
        out.cells = [v - level for v in r.cells]
        return out
    return r.local_contrast(max(1, int(round(scale_m / r.gsd_m))))


def suppress_overlaps(dets: list[Detection], iou_radius: float = 0.6
                      ) -> list[Detection]:
    """Keep the most confident detection where several describe one object.

    A multi-scale sweep reports the same warehouse from two or three bands.
    Without this, every count the product quotes is inflated by a factor that
    depends on how many scales happen to be enabled -- which would make the
    pattern-of-life baselines meaningless.
    """
    kept: list[Detection] = []
    for d in sorted(dets, key=lambda x: -x.confidence):
        clash = False
        for k in kept:
            #: Scaled by the *smaller* extent. Using the larger one lets a
            #: 2.4 km quay swallow every warehouse within a kilometre of it,
            #: which is not deduplication -- it is deleting the estate.
            reach = iou_radius * min(d.extent_m, k.extent_m)
            if haversine_m((d.lon, d.lat), (k.lon, k.lat)) < reach:
                clash = True
                break
        if not clash:
            kept.append(d)
    return kept


def apply_context_rules(dets: list[Detection]) -> list[Detection]:
    """Fix classifications that only context can settle.

    Container stacks do not occur alone. A single 35 m bright rectangle in the
    middle of a salt flat is a building; the same rectangle as one of forty in
    a port yard is a container stack. Shape cannot tell them apart at 10 m and
    no amount of model capacity will change that, because the information is
    genuinely not in the object -- it is in its neighbours.

    Reclassifying rather than deleting, and at reduced confidence, because the
    rule is a prior and not a measurement.
    """
    out: list[Detection] = []
    for d in dets:
        if d.object_class is not ObjectClass.CONTAINER_STACK:
            out.append(d)
            continue
        neighbours = sum(
            1 for o in dets
            if o.id != d.id and o.object_class is ObjectClass.CONTAINER_STACK
            and haversine_m((d.lon, d.lat), (o.lon, o.lat)) <= 400.0)
        if neighbours >= 2:
            out.append(d)
        else:
            out.append(replace(d, object_class=ObjectClass.BUILDING,
                               confidence=round(d.confidence * 0.8, 3)))
    return out


def _ring_cells(comp: Component, width: int, height: int,
                inner: int = 2, outer: int = 4) -> list[tuple[int, int]]:
    """Cells in an annulus just outside the component.

    Grown from the component's rim rather than from every cell in it: the same
    set, and orders of magnitude less work on a large region.
    """
    own = {(c, r) for c, r in comp.cells}
    ring: set[tuple[int, int]] = set()
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
                    continue        # too close: inside the excluded core
                ring.add(p)
    return sorted(ring)


#: Inner radius of the neighbourhood edge_drop compares against, in cells.
#:
#: 1 means "everything outside the component", including the shell immediately
#: against it; 2 and above make it a true annulus with a gap.
#:
#: The annulus is the tidier idea and it measures worse. Against the demo
#: estate, excluding the adjacent shell costs three points of recall, all of it
#: at the two sites with water — a hull or a quay sits against a dark, flat
#: background, and the cells right beside it are where the contrast actually
#: falls away. Skipping them samples further out, where the background has
#: already recovered, which makes every object look less sharply bounded than
#: it is.
#:
#: Worth being explicit that the original code arrived here by accident: its
#: exclusion was applied per source cell rather than against the whole
#: component, so for anything larger than a few cells it excluded nothing. The
#: value is kept because it measures better, not because it was inherited, and
#: `terrashield evaluate` is how to re-decide it on other imagery.
EDGE_RING_INNER = 1


def edge_drop(comp: Component, contrast: Raster) -> float:
    """How sharply the component's contrast falls away at its own boundary.

    This is the single measurement that separates a warehouse from a low rise
    in the ground. A built structure has a step edge: two cells outside it, the
    contrast is gone. A terrain undulation with the same area, elongation and
    fill decays over a hundred metres, so the annulus around it is still
    strongly signed.

    Without this term the detector reports twenty-five buildings on an empty
    salt flat -- shapes that pass every size and shape gate and are simply not
    objects. A monitoring product that does that is worse than no product,
    because the analyst has to check all twenty-five.
    """
    inside = sum(abs(contrast.get(c, r)) for c, r in comp.cells) / comp.pixel_count
    if inside < 1e-9:
        return 0.0
    ring = _ring_cells(comp, contrast.width, contrast.height,
                       inner=EDGE_RING_INNER)
    if not ring:
        return 0.0
    outside = sum(abs(contrast.get(c, r)) for c, r in ring) / len(ring)
    return max(0.0, 1.0 - outside / inside)


def _surrounded_by_water(comp: Component, water: Mask) -> bool:
    """Is this object sitting *in* water?

    Tested on the ring around the component rather than on its own cells. A
    ship is bright and is therefore not itself classified as water, so asking
    whether the ship's pixels are water returns no for every ship afloat --
    which is how a harbour full of vessels comes back empty.
    """
    #: Sampled three to seven cells out, not immediately adjacent. The water
    #: mask is built from a blurred scene, so the two cells either side of a
    #: bright hull are pulled above the water threshold by the hull itself.
    #: Testing there asks whether the ship is floating on ship, and the answer
    #: is always no -- which silently empties every harbour in the estate.
    ring = _ring_cells(comp, water.width, water.height, inner=3, outer=7)
    if not ring:
        return False
    return sum(1 for c, r in ring if water.get(c, r)) / len(ring) > 0.5


def _classify(aoi: Aoi, scene: Scene, r: Raster, contrast: Raster,
              comp: Component, polarity: int, water: Mask, frame,
              noise: float, min_confidence: float) -> Detection | None:
    c0, r0, c1, r1 = comp.bbox_cells
    gsd = comp.gsd_m
    extent_m, minor_m, _ = comp.axes
    elong = comp.elongation
    fill = comp.fill
    over_water = _surrounded_by_water(comp, water)

    best: tuple[float, ClassProfile] | None = None
    runner: float = 0.0
    for p in PROFILES:
        s = p.score(extent_m, elong, fill, polarity, over_water)
        if s <= 0:
            continue
        if best is None or s > best[0]:
            runner = best[0] if best else 0.0
            best = (s, p)
        elif s > runner:
            runner = s
    if best is None:
        return None

    fit, profile = best
    magnitude = sum(abs(contrast.get(c, rr)) for c, rr in comp.cells) / comp.pixel_count
    snr = magnitude / max(noise, 1e-6)

    #: Confidence has four factors and each can veto. Signal-to-noise says the
    #: thing is there at all; edge sharpness says it is an object rather than a
    #: feature of the landscape; shape fit says it is this class; the ambiguity
    #: penalty says no other class fits nearly as well. Multiplying them means a
    #: strong blob that could equally be a truck or a container stack does not
    #: get reported at 0.9 -- which is the failure mode that destroys analyst
    #: trust faster than a missed detection ever does.
    snr_term = min(1.0, max(0.0, (snr - 2.0) / 5.0))
    ambiguity = 1.0 - 0.45 * (runner / fit if fit > 0 else 0.0)
    drop = edge_drop(comp, contrast)
    sharpness = min(1.0, max(0.0, (drop - 0.25) / 0.45))
    confidence = round(min(0.97, snr_term * fit * ambiguity
                           * (0.15 + 0.85 * sharpness)), 3)
    if confidence < min_confidence:
        return None

    ce, cn = comp.centroid_cell
    east = r.origin_e + (ce + 0.5) * gsd
    north = r.origin_n - (cn + 0.5) * gsd
    lon, lat = frame.to_lonlat(east, north)
    half_l, half_w = ((c1 - c0 + 1) * gsd) / 2, ((r1 - r0 + 1) * gsd) / 2
    ring = frame.ring_to_lonlat([
        (east - half_l, north - half_w), (east + half_l, north - half_w),
        (east + half_l, north + half_w), (east - half_l, north + half_w),
    ])

    det_id = "det-" + format(seed_of(scene.id, profile.object_class.value,
                                     round(east, 1), round(north, 1)) & 0xFFFFFFFFFFFF,
                             "012x")
    return Detection(
        id=det_id, aoi_id=aoi.id, scene_id=scene.id,
        object_class=profile.object_class, confidence=confidence,
        lon=round(lon, 7), lat=round(lat, 7), geometry=ring,
        extent_m=round(extent_m, 1), model_version=MODEL_VERSION,
        observed_at=scene.acquired_at,
    )


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

@dataclass
class CalibrationBin:
    low: float
    high: float
    n: int
    correct: int

    @property
    def observed(self) -> float:
        return self.correct / self.n if self.n else 0.0

    @property
    def expected(self) -> float:
        return (self.low + self.high) / 2

    @property
    def gap(self) -> float:
        return self.observed - self.expected


def calibration_report(pairs: list[tuple[float, bool]],
                       edges: tuple[float, ...] = (0.35, 0.5, 0.65, 0.8, 1.01),
                       ) -> list[CalibrationBin]:
    """Does a confidence of 0.8 mean right eight times in ten?

    `pairs` is (confidence, was_correct) from scoring detections against known
    truth. PRD section 54 lists model confidence calibration as a success
    metric and it is the right instinct: an analyst learns within a week
    whether the numbers mean anything, and if they do not, they start ignoring
    all of them, including the ones that were right.
    """
    bins = [CalibrationBin(lo, hi, 0, 0) for lo, hi in zip(edges, edges[1:])]
    for conf, ok in pairs:
        for b in bins:
            if b.low <= conf < b.high:
                b.n += 1
                b.correct += 1 if ok else 0
                break
    return bins
