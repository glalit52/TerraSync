"""Does the pipeline recover what is actually on the ground?

These are the tests that make the product's claims checkable. `world.py` holds
the truth -- which structures exist on which date, how many vehicles are parked
where, when construction starts -- and the engines are never given it. Here we
score what they got back.

The numbers asserted below are floors, not targets, and they are deliberately
a little under the measured values so that ordinary variation does not turn the
suite red. What must not happen silently is a *regression*: if detection recall
drops below 0.70 or the false-positive rate doubles, that is a product change,
and it should fail here rather than be discovered by a customer.

They are also honest about scope: recall is measured against objects the sensor
could actually resolve. Scoring a 10 m Sentinel-2 scene for failing to find
cars would produce a number that says nothing about the detector.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from terrashield import change, detect, evaluate, sites
from terrashield.catalog import SyntheticProvider, best_pair, coverage
from terrashield.domain import ChangeType, Sensor, Severity
from terrashield.raster import Mask, Raster
from terrashield.world import Rng

WINDOW = (date(2026, 2, 1), date(2026, 3, 31))


@pytest.fixture(scope="module")
def provider() -> SyntheticProvider:
    p = SyntheticProvider()
    for demo in sites.load_all():
        p.register(demo.truth, demo.climate)
    return p


@pytest.fixture(scope="module")
def estate() -> list:
    return sites.load_all()


@pytest.fixture(scope="module")
def detection_score(provider, estate) -> evaluate.Score:
    """One scored sweep, reused by every assertion below. It is the slow part."""
    total = evaluate.Score()
    for demo in estate:
        scenes = [s for s in provider.search(demo.aoi, *WINDOW)
                  if s.usable and s.sensor is Sensor.OPTICAL][:3]
        for scene in scenes:
            r = provider.fetch(demo.aoi, scene)
            masks = provider.masks(demo.aoi, scene, r)
            result = detect.detect(demo.aoi, scene, r, obscured=masks.cloud,
                                   water=masks.water)
            total.add(evaluate.score_scene(
                demo.truth, demo.aoi.boundary, scene.acquired_on,
                result.detections, min_extent_m=2.5 * r.gsd_m,
                min_width_m=1.5 * r.gsd_m, area_km2=demo.aoi.area_km2))
    return total


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def test_detection_recall_and_precision_hold(detection_score):
    assert detection_score.matched > 30, "too few resolvable objects to conclude"
    assert detection_score.recall >= 0.70
    assert detection_score.precision >= 0.70


def test_false_positive_density_stays_low(detection_score):
    """The number that decides whether an analyst can work the output at all."""
    assert detection_score.false_positives_per_km2 <= 0.15


def test_confidence_is_calibrated_at_the_top_of_the_range(detection_score):
    """A detection at 0.8 has to be right about eight times in ten."""
    bins = detect.calibration_report(detection_score.confidence_pairs)
    top = [b for b in bins if b.low >= 0.8 and b.n >= 8]
    assert top, "not enough high-confidence detections to judge calibration"
    for b in top:
        assert b.observed >= 0.75


def test_detector_declares_what_the_sensor_cannot_resolve(provider):
    """A count of zero cars from a 10 m scene must not read as 'there were none'."""
    demo = sites.load("mundra")
    scene = next(s for s in provider.search(demo.aoi, *WINDOW)
                 if s.usable and s.sensor is Sensor.OPTICAL)
    r = provider.fetch(demo.aoi, scene)
    result = detect.detect(demo.aoi, scene, r)
    skipped = {s.object_class.value: s for s in result.skipped}
    assert "vehicle" in skipped
    assert not result.reportable(detect.ObjectClass.VEHICLE)
    assert skipped["vehicle"].needs_gsd_m < 4.0
    assert "10.0 m ground sample distance" in skipped["vehicle"].reason


def test_vessels_are_found_at_the_port(provider):
    """Ships lie at arbitrary headings; the shape descriptors must not care."""
    demo = sites.load("mundra")
    scene = next(s for s in provider.search(demo.aoi, *WINDOW)
                 if s.usable and s.sensor is Sensor.OPTICAL)
    r = provider.fetch(demo.aoi, scene)
    masks = provider.masks(demo.aoi, scene, r)
    result = detect.detect(demo.aoi, scene, r, obscured=masks.cloud,
                           water=masks.water)
    truth = sum(1 for f in demo.truth.transients_on(scene.acquired_on)
                if f.object_class.value == "vessel")
    found = result.count_of(detect.ObjectClass.VESSEL)
    assert truth >= 5
    assert found >= truth * 0.4, f"found {found} of {truth} vessels"


def test_solar_arrays_are_found_at_the_energy_site(provider):
    """Large uniform objects need the coarse end of the scale sweep."""
    demo = sites.load("bhadla")
    scene = next(s for s in provider.search(demo.aoi, *WINDOW)
                 if s.usable and s.sensor is Sensor.OPTICAL)
    r = provider.fetch(demo.aoi, scene)
    masks = provider.masks(demo.aoi, scene, r)
    result = detect.detect(demo.aoi, scene, r, obscured=masks.cloud,
                           water=masks.water)
    assert result.count_of(detect.ObjectClass.SOLAR_ARRAY) >= 4


def test_an_empty_sector_does_not_manufacture_structures(provider):
    """A near-empty background is the hardest test of a false-positive rate."""
    demo = sites.load("kutch")
    total_fp = 0
    scenes = [s for s in provider.search(demo.aoi, *WINDOW)
              if s.usable and s.sensor is Sensor.OPTICAL][:3]
    for scene in scenes:
        r = provider.fetch(demo.aoi, scene)
        masks = provider.masks(demo.aoi, scene, r)
        result = detect.detect(demo.aoi, scene, r, obscured=masks.cloud,
                               water=masks.water)
        total_fp += len(result.detections)
    assert scenes
    per_scene = total_fp / len(scenes)
    assert per_scene <= 6, f"{per_scene:.1f} detections per look on empty terrain"


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------

def _compare(provider, demo, before, after):
    rb, ra = provider.fetch(demo.aoi, before), provider.fetch(demo.aoi, after)
    mb = provider.masks(demo.aoi, before, rb)
    ma = provider.masks(demo.aoi, after, ra)
    return change.compare(demo.aoi, before, after, rb, ra, mb.cloud, ma.cloud,
                          mb.water, ma.water)


def _pick(provider, demo, start, end, sensor=Sensor.OPTICAL):
    pool = [s for s in provider.search(demo.aoi, start, end)
            if s.usable and s.sensor is sensor]
    assert len(pool) >= 2, "need two usable scenes to compare"
    return pool[0], pool[-1]


def test_a_new_solar_block_is_found_and_called_a_structure(provider):
    """BHD-ARR-NEW-0 is commissioned on 2026-03-19. It is 228,800 m2."""
    demo = sites.load("bhadla")
    before, after = _pick(provider, demo, date(2026, 3, 1), date(2026, 4, 20))
    result = _compare(provider, demo, before, after)
    assert result.usable, result.refusal
    structural = [e for e in result.events
                  if e.change_type is ChangeType.NEW_STRUCTURE]
    assert structural, [e.change_type.value for e in result.events]
    biggest = max(structural, key=lambda e: e.area_m2)
    assert biggest.area_m2 > 80_000
    assert biggest.confidence >= 0.7
    assert biggest.severity.rank >= Severity.MEDIUM.rank


def test_a_dark_new_structure_is_not_called_inundation(provider):
    """Photovoltaic panels are as dark as water in the visible bands.

    Without the provider's water band this reported 11.7 hectares of flooding
    on a desert energy site, at high severity.
    """
    demo = sites.load("bhadla")
    before, after = _pick(provider, demo, date(2026, 3, 1), date(2026, 4, 20))
    result = _compare(provider, demo, before, after)
    assert not any(e.change_type is ChangeType.INUNDATION for e in result.events)


def test_reservoir_drawdown_is_measured_as_area(provider):
    """SSD-DRAWDOWN exposes bank from 2026-04-07. Physical, and checkable."""
    demo = sites.load("sardar")
    before, after = _pick(provider, demo, date(2026, 3, 20), date(2026, 5, 10))
    result = _compare(provider, demo, before, after)
    assert result.usable, result.refusal
    big = [e for e in result.events if e.area_m2 > 500_000]
    assert big, [(e.change_type.value, e.area_m2) for e in result.events[:5]]
    assert any("water" in e.explanation for e in big)


def test_vessel_movement_is_activity_not_a_change_event(provider):
    """A container terminal turns over dozens of ships between passes."""
    demo = sites.load("mundra")
    before, after = _pick(provider, demo, date(2026, 4, 1), date(2026, 5, 20))
    result = _compare(provider, demo, before, after)
    assert result.movements, "no vessel movement recognised at a working port"
    assert result.arrivals + result.departures == len(result.movements)
    assert not any(e.change_type in change.MOVEMENT_TYPES for e in result.events)


def test_a_cluster_of_departing_vessels_is_not_flooding(provider):
    """Berthed ships cluster; a row of them leaving merges into one region.

    Capping the afloat test by area sent exactly those to the inundation
    branch, and the port's feed carried "13.6 ha changed from a land signature
    to a water signature" at high severity four times in a fortnight.
    """
    demo = sites.load("mundra")
    #: The scripted congestion episode runs 2026-05-11 to 2026-05-26, so the
    #: comparison spanning its end is where the merged departures happen.
    before, after = _pick(provider, demo, date(2026, 5, 10), date(2026, 6, 5))
    result = _compare(provider, demo, before, after)
    assert result.usable, result.refusal

    big_water = [e for e in result.events
                 if e.change_type is ChangeType.INUNDATION and e.area_m2 > 40_000]
    assert not big_water, [e.explanation for e in big_water]

    merged = [e for e in result.movements if e.area_m2 > 40_000]
    if merged:
        assert any("several moving together" in e.explanation for e in merged)


def test_sar_finds_a_new_building_when_optical_is_blind(provider):
    """The monsoon case, and the reason SAR is in the constellation mix."""
    demo = sites.load("kutch")
    window = (date(2026, 6, 20), date(2026, 9, 10))
    scenes = provider.search(demo.aoi, *window)
    optical = [s for s in scenes if s.usable and s.sensor is Sensor.OPTICAL]
    assert len(optical) <= 2, "this window is supposed to be cloud-bound"

    pair = best_pair(scenes, sensor=Sensor.SAR, min_separation_days=30)
    assert pair is not None
    result = _compare(provider, demo, *pair)
    assert result.usable, result.refusal
    structural = [e for e in result.events
                  if e.change_type in (ChangeType.NEW_STRUCTURE,
                                       ChangeType.CONSTRUCTION_ACTIVITY)]
    assert structural, [e.change_type.value for e in result.events]


def test_change_refuses_a_cross_constellation_pair(provider):
    demo = sites.load("bhadla")
    scenes = provider.search(demo.aoi, date(2026, 2, 1), date(2026, 3, 31))
    s2 = next(s for s in scenes if s.constellation.value == "sentinel-2" and s.usable)
    l9 = next(s for s in scenes if s.constellation.value == "landsat-9" and s.usable)
    before, after = sorted([s2, l9], key=lambda s: s.acquired_at)
    r = provider.fetch(demo.aoi, before)
    result = change.compare(demo.aoi, before, after, r, r)
    assert not result.usable
    assert "ground sample distance" in result.refusal


def test_change_refuses_sar_across_ground_tracks(provider):
    demo = sites.load("kutch")
    sar = [s for s in provider.search(demo.aoi, date(2026, 6, 1), date(2026, 7, 31))
           if s.sensor is Sensor.SAR]
    tracks = {s.orbit for s in sar}
    assert len(tracks) > 1, "the orbit model should produce more than one track"
    a = sar[0]
    b = next(s for s in sar if s.orbit != a.orbit and s.acquired_at > a.acquired_at)
    r = provider.fetch(demo.aoi, a)
    result = change.compare(demo.aoi, a, b, r, r)
    assert not result.usable
    assert "orbit track" in result.refusal


def test_coregistration_recovers_a_deliberate_shift(provider):
    """Scenes carry a modelled geolocation error; alignment has to remove it."""
    demo = sites.load("bhadla")
    before, after = _pick(provider, demo, date(2026, 2, 1), date(2026, 3, 31))
    rb = provider.fetch(demo.aoi, before)
    shifted = rb.shifted(2, -1)
    d_col, d_row, quality = change.coregister(rb, shifted)
    assert (d_col, d_row) == (2, -1)
    assert quality > 0.2


def test_cloud_is_kept_out_of_the_registration_score(provider):
    """Cloud in one scene only must not decide where the other one sits.

    Cloud is bright, it is in one scene and not the other, and it does not
    move with the ground, so it adds a large near-constant term to every
    candidate shift. That flattens the alignment curve until the minimum is
    decided by noise -- and a scene displaced on noise puts a bright rim along
    one side of every static building and a dark rim along the other.
    """
    demo = sites.load("bhadla")
    before, _ = _pick(provider, demo, date(2026, 2, 1), date(2026, 3, 31))
    rb = provider.fetch(demo.aoi, before)
    shifted = rb.shifted(1, -1)

    #: A bright bank of cloud over a third of the after scene, present nowhere
    #: in the before scene.
    clouded = shifted.copy()
    cloud = Mask.like(rb)
    for row in range(rb.height):
        for col in range(rb.width // 3):
            clouded.set(col, row, 0.92)
            cloud.set(col, row, True)

    blind = change.coregister(rb, clouded)
    seeing = change.coregister(rb, clouded, exclude=cloud)
    assert seeing[:2] == (1, -1), (
        f"with the cloud excluded the true shift should be recovered, got {seeing}")
    assert blind[:2] != (1, -1) or blind[2] < seeing[2], (
        "the cloud should measurably degrade the score it is included in")


def test_a_shift_is_not_applied_without_evidence_for_it(provider):
    """An aligned pair must be left alone, however flat the search surface.

    The failure this pins was found by running the demo estate, not by review:
    a Landsat pair over Sardar Sarovar with 6% cloud before and 29% after
    scored 0.12108 to 0.12186 across seven candidate row shifts -- a 0.06%
    spread -- and the argmin of that noise was applied as a two-row shift. It
    produced five changes at a site where nothing was built, four of them
    hugging the edges of the dam wall and the powerhouse, one reported at high
    severity as "a uniform engineered surface has replaced a varied natural
    one".
    """
    demo = sites.load("sardar")
    scenes = {s.id: s for s in provider.search(
        demo.aoi, date(2026, 2, 20), date(2026, 4, 5))}
    before = scenes["landsat-9:IN-SSD-DAM:2026-02-26"]
    after = scenes["landsat-9:IN-SSD-DAM:2026-03-30"]
    rb = provider.fetch(demo.aoi, before)
    ra = provider.fetch(demo.aoi, after)
    mb = provider.masks(demo.aoi, before, rb)
    ma = provider.masks(demo.aoi, after, ra)

    result = change.compare(demo.aoi, before, after, rb, ra,
                            mb.cloud, ma.cloud, mb.water, ma.water)
    assert result.usable, result.refusal
    assert result.registration_shift == (0, -1), result.registration_shift
    assert not result.events, [e.explanation for e in result.events]


def test_a_flat_search_surface_yields_no_shift():
    """Uniform noise gives the estimator nothing; it must decline, not guess."""
    grid = Raster.like(Raster(40, 40, 10.0, 0.0, 0.0, [0.0] * 1600))
    rng = Rng(4)
    a = grid.copy()
    b = grid.copy()
    for i in range(len(a.cells)):
        a.cells[i] = 0.4 + rng.gauss(0.0, 0.01)
        b.cells[i] = 0.4 + rng.gauss(0.0, 0.01)
    d_col, d_row, quality = change.coregister(a, b)
    assert (d_col, d_row) == (0, 0)
    assert quality < change.MIN_REGISTRATION_QUALITY


def test_normalisation_removes_an_illumination_difference(provider):
    """Sun angle moves a scene more than most construction does."""
    demo = sites.load("bhadla")
    before, _ = _pick(provider, demo, date(2026, 2, 1), date(2026, 3, 31))
    rb = provider.fetch(demo.aoi, before)
    brighter = rb.scaled(1.22, 0.03)
    gain, offset = change.normalise(brighter, rb)
    corrected = brighter.scaled(gain, offset)
    residual = corrected.difference(rb)
    assert abs(residual.median()) < 0.01


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------

def test_monsoon_coverage_collapses_for_optical_and_sar_carries_it(provider):
    """The commercial fact behind the constellation mix, asserted."""
    demo = sites.load("mundra")
    window = (date(2026, 6, 1), date(2026, 9, 30))
    scenes = provider.search(demo.aoi, *window)
    cov = coverage(demo.aoi.id, scenes, *window)
    assert cov.usable_fraction < 0.65
    assert cov.by_constellation.get("sentinel-1", 0) > \
        cov.by_constellation.get("sentinel-2", 0) * 2
    assert cov.rejected_reasons.get("cloud", 0) > 10


def test_dry_season_optical_coverage_is_good(provider):
    demo = sites.load("bhadla")
    window = (date(2026, 1, 1), date(2026, 3, 31))
    cov = coverage(demo.aoi.id, provider.search(demo.aoi, *window), *window)
    assert cov.usable_fraction > 0.7
    assert cov.longest_gap_days <= 10


def test_the_raster_cache_is_bounded(provider):
    """An unbounded scene cache is a slow leak with a monitoring workload."""
    demo = sites.load("kutch")
    scenes = [s for s in provider.search(demo.aoi, date(2026, 1, 1),
                                         date(2026, 6, 30)) if s.usable]
    assert len(scenes) > provider.cache_size
    for scene in scenes:
        provider.fetch(demo.aoi, scene)
    assert len(provider._cache) <= provider.cache_size


def test_rendering_is_deterministic(provider):
    """Two runs of the same scene must be byte-identical, forever."""
    demo = sites.load("kutch")
    scene = next(s for s in provider.search(demo.aoi, *WINDOW) if s.usable)
    first = provider.fetch(demo.aoi, scene)
    fresh = SyntheticProvider()
    fresh.register(sites.load("kutch").truth, demo.climate)
    second = fresh.fetch(demo.aoi, scene)
    assert first.cells == second.cells
