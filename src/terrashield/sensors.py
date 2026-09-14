"""The imaging model: how a place on the ground becomes a grid of numbers.

The point of modelling the sensor rather than drawing a picture is that the
limits come out right. Three of them drive real product decisions:

**Resolution is linear mixing, not blur.** A 4.5 m car inside a 10 m Sentinel-2
pixel contributes 8.5% of that pixel's value, so it is invisible under any
detector. This module reproduces that by weighting each object's contribution
by its true fractional coverage of the cell. The consequence -- that
vehicle-count pattern-of-life needs tasked sub-metre imagery and cannot be sold
on free Sentinel data -- is a commercial fact, and a demo that hid it would be
selling something that does not exist.

**Sun angle is a change signal if you let it be.** The same site imaged in
December and June differs in brightness by more than most construction does.
That is why change.py normalises radiometrically before differencing, and why
this module bothers to vary illumination at all.

**SAR speckle is multiplicative.** It does not average out the way additive
noise does, and a detector tuned on optical imagery falls apart on it. Modelled
here so the engines have to cope.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from .domain import Constellation, Scene, Sensor
from .raster import Mask, Raster
from .world import Rng, SiteTruth, fbm, height_of, seed_of


@dataclass(frozen=True)
class SensorModel:
    """Radiometric character of one constellation."""

    constellation: Constellation
    sensor: Sensor
    gsd_m: float
    noise_sd: float               # additive sensor noise, in reflectance units
    speckle: float = 0.0          # multiplicative SAR speckle, 0 for optical
    psf_radius_cells: int = 0     # optical point spread, in cells

    @property
    def detectable_extent_m(self) -> float:
        """Smallest object extent a detector can honestly claim to find.

        Two-and-a-bit pixels across. Below this an object is a fraction of one
        cell's value and indistinguishable from noise, whatever a model claims.
        """
        return 2.5 * self.gsd_m


SENSORS: dict[Constellation, SensorModel] = {
    Constellation.SENTINEL_2: SensorModel(
        Constellation.SENTINEL_2, Sensor.OPTICAL, gsd_m=10.0,
        noise_sd=0.008, psf_radius_cells=1),
    Constellation.LANDSAT_9: SensorModel(
        Constellation.LANDSAT_9, Sensor.OPTICAL, gsd_m=30.0,
        noise_sd=0.010, psf_radius_cells=1),
    Constellation.SENTINEL_1: SensorModel(
        Constellation.SENTINEL_1, Sensor.SAR, gsd_m=10.0,
        noise_sd=0.004, speckle=0.22),
    Constellation.COMMERCIAL_VHR: SensorModel(
        Constellation.COMMERCIAL_VHR, Sensor.OPTICAL, gsd_m=0.5,
        noise_sd=0.012, psf_radius_cells=1),
}


# ---------------------------------------------------------------------------
# Stamping
# ---------------------------------------------------------------------------

def _corners(east: float, north: float, length: float, width: float,
             heading_deg: float) -> list[tuple[float, float]]:
    a = math.radians(heading_deg)
    ca, sa = math.cos(a), math.sin(a)
    hl, hw = length / 2, width / 2
    return [
        (east + dx * ca - dy * sa, north + dx * sa + dy * ca)
        for dx, dy in ((-hl, -hw), (hl, -hw), (hl, hw), (-hl, hw))
    ]


def _inside_rect(pt, corners) -> bool:
    x, y = pt
    sign = 0
    for (x1, y1), (x2, y2) in zip(corners, corners[1:] + corners[:1]):
        cross = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)
        s = (cross > 0) - (cross < 0)
        if s == 0:
            continue
        if sign == 0:
            sign = s
        elif s != sign:
            return False
    return True


def stamp_rect(r: Raster, east: float, north: float, length: float, width: float,
               heading_deg: float, value: float, samples: int = 4) -> None:
    """Blend `value` into the raster weighted by each cell's true coverage.

    The supersampling is what makes sub-pixel objects behave correctly. Drop it
    for speed and a car in a Sentinel-2 pixel becomes either a full bright cell
    or nothing, and every resolution claim the product makes becomes false.
    """
    corners = _corners(east, north, length, width, heading_deg)
    es = [c[0] for c in corners]
    ns = [c[1] for c in corners]
    c0, r0 = r.to_cell(min(es), max(ns))
    c1, r1 = r.to_cell(max(es), min(ns))
    step = 1.0 / samples
    denom = samples * samples
    for row in range(max(0, r0), min(r.height, r1 + 2)):
        for col in range(max(0, c0), min(r.width, c1 + 2)):
            ce, cn = r.to_m(col, row)
            hit = 0
            for sy in range(samples):
                for sx in range(samples):
                    px = ce + (sx * step + step / 2 - 0.5) * r.gsd_m
                    py = cn + (sy * step + step / 2 - 0.5) * r.gsd_m
                    if _inside_rect((px, py), corners):
                        hit += 1
            if hit:
                cov = hit / denom
                r.set(col, row, r.get(col, row) * (1 - cov) + value * cov)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _grid_for(truth: SiteTruth, extent_m: tuple[float, float, float, float],
              gsd_m: float, band: str) -> Raster:
    min_e, min_n, max_e, max_n = extent_m
    width = max(8, int(round((max_e - min_e) / gsd_m)))
    height = max(8, int(round((max_n - min_n) / gsd_m)))
    return Raster(width, height, gsd_m, min_e, max_n, band=band)


def geolocation_error_m(scene: Scene) -> tuple[float, float]:
    """Where this scene actually landed relative to the map, in metres.

    Real products are not perfectly registered. Sentinel-2 L1C is specified at
    about 11 m at 95% confidence before refinement, and off-nadir acquisitions
    are worse because terrain parallax scales with view angle. Modelled here
    because a change detector that is never handed a misregistered pair is
    never tested on the failure that produces most of its false positives: one
    cell of shift puts a bright rim on one side of every building in the AOI
    and a dark rim on the other.
    """
    rng = Rng(seed_of(scene.id, "geoloc"))
    scale = 6.0 + 0.6 * scene.off_nadir_deg
    return rng.gauss(0.0, scale), rng.gauss(0.0, scale)


def render(truth: SiteTruth, scene: Scene,
           extent_m: tuple[float, float, float, float],
           gsd_m: float | None = None) -> Raster:
    """Render one scene over one site. The only bridge from truth to pixels.

    Everything downstream -- detection, change, baselines, alerts -- sees only
    what comes out of here.
    """
    model = SENSORS[scene.constellation]
    gsd = gsd_m or model.gsd_m
    when = scene.acquired_on
    de, dn = geolocation_error_m(scene)
    shifted = (extent_m[0] + de, extent_m[1] + dn,
               extent_m[2] + de, extent_m[3] + dn)
    if model.sensor is Sensor.SAR:
        r = _render_sar(truth, when, shifted, gsd, scene)
    else:
        r = _render_optical(truth, when, shifted, gsd, scene, model)
    #: Put the grid back on the nominal footprint. The pixels now carry the
    #: acquisition's own geolocation error, which is exactly the situation
    #: co-registration exists to fix.
    r.origin_e, r.origin_n = extent_m[0], extent_m[3]
    return r.clipped(0.0, 1.0)


def _render_optical(truth: SiteTruth, when: date,
                    extent_m: tuple[float, float, float, float], gsd: float,
                    scene: Scene, model: SensorModel) -> Raster:
    r = _grid_for(truth, extent_m, gsd, "optical")

    # Terrain.
    for row in range(r.height):
        for col in range(r.width):
            e, n = r.to_m(col, row)
            r.set(col, row, truth.terrain.optical(e, n))

    # Engineered surfaces (aprons, yards, roads) sit on top of terrain.
    for z in truth.zones:
        if z.surface_reflectance is not None:
            stamp_rect(r, z.east_m, z.north_m, z.length_m, z.width_m, 0.0,
                       z.surface_reflectance)

    # Illumination. Sun elevation drives both overall brightness and shadows.
    sun_el = max(scene.sun_elevation_deg, 5.0)
    brightness = 0.55 + 0.45 * math.sin(math.radians(sun_el))
    shadow_len = min(60.0, 1.0 / math.tan(math.radians(sun_el)))
    sun_az = 135.0 + 30.0 * math.sin(when.timetuple().tm_yday / 365 * 2 * math.pi)
    sx = -math.sin(math.radians(sun_az))
    sy = -math.cos(math.radians(sun_az))

    persistent = truth.features_on(when)
    for f in persistent + truth.transients_on(when):
        h = height_of(f)
        if h > 0 and shadow_len > 0:
            d = h * shadow_len
            stamp_rect(r, f.east_m + sx * d / 2, f.north_m + sy * d / 2,
                       f.length_m, max(f.width_m, d), f.heading_deg, 0.08)
        stamp_rect(r, f.east_m, f.north_m, f.length_m, f.width_m,
                   f.heading_deg, f.reflectance)

    r = r.scaled(brightness)
    if model.psf_radius_cells:
        r = r.box_blur(model.psf_radius_cells)

    if scene.cloud_pct > 0.5:
        _apply_cloud(r, scene, truth.seed)
    _apply_noise(r, model.noise_sd, scene.id)
    return r


def _render_sar(truth: SiteTruth, when: date,
                extent_m: tuple[float, float, float, float], gsd: float,
                scene: Scene) -> Raster:
    model = SENSORS[scene.constellation]
    r = _grid_for(truth, extent_m, gsd, "sar")
    for row in range(r.height):
        for col in range(r.width):
            e, n = r.to_m(col, row)
            r.set(col, row, truth.terrain.sar(e, n))
    for z in truth.zones:
        if z.surface_backscatter is not None:
            stamp_rect(r, z.east_m, z.north_m, z.length_m, z.width_m, 0.0,
                       z.surface_backscatter)
    for f in truth.features_on(when) + truth.transients_on(when):
        stamp_rect(r, f.east_m, f.north_m, f.length_m, f.width_m,
                   f.heading_deg, f.backscatter)
        #: Layover: a vertical wall facing the radar returns a bright line in
        #: front of the structure. It is why SAR finds buildings so reliably
        #: and why a SAR footprint is offset from the optical one.
        if height_of(f) > 4:
            stamp_rect(r, f.east_m - 0.5 * f.length_m, f.north_m,
                       max(gsd, 0.2 * f.length_m), f.width_m, f.heading_deg,
                       min(1.0, f.backscatter * 1.25))

    rng = Rng(seed_of(scene.id, "speckle"))
    if model.speckle:
        for i, v in enumerate(r.cells):
            #: Multiplicative and heavy-tailed, as speckle actually is.
            r.cells[i] = v * max(0.05, 1.0 + rng.gauss(0.0, model.speckle))
    _apply_noise(r, model.noise_sd, scene.id)
    return r


def cloud_opacity(r: Raster, scene: Scene) -> list[float]:
    """Per-cell cloud opacity, 0..1, on the geometry of `r`.

    Cloud is a field with a threshold rather than a uniform haze, so a scene
    reported as 25% cloudy is genuinely usable over three quarters of the AOI
    and genuinely blind over the rest. That is the situation an analyst is
    actually in, and it is why partial-coverage handling exists at all.
    """
    if scene.sensor is not Sensor.OPTICAL or scene.cloud_pct <= 0.5:
        return [0.0] * len(r.cells)
    seed = seed_of(scene.id, "cloud")
    target = scene.cloud_pct / 100.0
    field_vals = []
    for row in range(r.height):
        for col in range(r.width):
            e, n = r.to_m(col, row)
            field_vals.append(fbm(seed, e, n, 900.0, octaves=3))
    ordered = sorted(field_vals)
    cut = ordered[min(len(ordered) - 1, int((1 - target) * len(ordered)))]
    return [min(1.0, max(0.0, (v - cut) / 0.12)) for v in field_vals]


def _apply_cloud(r: Raster, scene: Scene, site_seed: int) -> None:
    for i, op in enumerate(cloud_opacity(r, scene)):
        if op > 0:
            r.cells[i] = r.cells[i] * (1 - op) + 0.92 * op


def _apply_noise(r: Raster, sd: float, scene_id: str) -> None:
    if sd <= 0:
        return
    rng = Rng(seed_of(scene_id, "noise"))
    for i in range(len(r.cells)):
        r.cells[i] += rng.gauss(0.0, sd)


def water_truth_mask(truth: SiteTruth, when: date, r: Raster) -> Mask:
    """The scene classification water band, as a provider would deliver it.

    Not derived from brightness, because brightness cannot do this job. A
    photovoltaic array and a reservoir are both very dark in the visible bands;
    the thing that separates them is the near infrared, where water absorbs
    almost everything and a panel field does not. Sentinel-2 ships exactly this
    discrimination in its scene classification layer and Landsat ships QA_PIXEL,
    so a pipeline that re-derives water from a single visible band is choosing a
    worse answer than the one already in the product.

    The consequence of not doing this is specific and was observed: a newly
    commissioned solar block was reported as 11.7 hectares of inundation, at
    high severity, on an energy site in a desert.
    """
    m = Mask.like(r)
    for row in range(r.height):
        for col in range(r.width):
            e, n = r.to_m(col, row)
            m.set(col, row, truth.terrain.is_water(e, n))
    #: Anything standing on the water is not water: a hull, a pier, a barge,
    #: or -- at a reservoir in drawdown -- the exposed bank itself.
    for f in truth.features_on(when) + truth.transients_on(when):
        _clear_rect(m, f.east_m, f.north_m, f.length_m, f.width_m, f.heading_deg)
    return m


def _clear_rect(m: Mask, east: float, north: float, length: float, width: float,
                heading_deg: float) -> None:
    corners = _corners(east, north, length, width, heading_deg)
    es = [c[0] for c in corners]
    ns = [c[1] for c in corners]
    c0 = int((min(es) - m.origin_e) / m.gsd_m)
    r0 = int((m.origin_n - max(ns)) / m.gsd_m)
    c1 = int((max(es) - m.origin_e) / m.gsd_m)
    r1 = int((m.origin_n - min(ns)) / m.gsd_m)
    for row in range(max(0, r0), min(m.height, r1 + 2)):
        for col in range(max(0, c0), min(m.width, c1 + 2)):
            e, n = m.to_m(col, row)
            if _inside_rect((e, n), corners):
                m.set(col, row, False)


def cloud_mask(r: Raster, scene: Scene, threshold: float = 0.10) -> Mask:
    """Which cells are cloud-obscured, as the imagery provider would report it.

    Deliberately not re-derived from the pixels. Sentinel-2 ships a scene
    classification band, Planet ships a usable-data mask, and a pipeline that
    ignores them in favour of its own brightness heuristic is choosing a worse
    mask for no reason. It is also choosing a dangerous one: a heuristic tuned
    to catch thick cloud misses thin cloud over water, and a bright flat patch
    on a dark reservoir is indistinguishable from a large building. That single
    gap produced most of the false structures in the first version of this
    engine.

    A real mask is imperfect at the edges, so this one is dilated by a cell --
    the same conservative bias the providers apply, for the same reason: a
    missed change costs one look, a fabricated one costs an analyst's afternoon.
    """
    m = Mask.like(r)
    op = cloud_opacity(r, scene)
    m.bits = [v >= threshold for v in op]
    return m.dilate(1) if any(m.bits) else m
