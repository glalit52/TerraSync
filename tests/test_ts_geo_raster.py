"""Geometry and the image algebra everything else is built on."""

from __future__ import annotations

import math
import struct
import zlib

import pytest

from terrashield import geo
from terrashield.detect import _ring_cells
from terrashield.raster import (
    Mask, Raster, encode_png, label_components, percentile, render_png,
    threshold,
)


# ---------------------------------------------------------------------------
# geo
# ---------------------------------------------------------------------------

def test_rectangle_area_matches_its_ground_dimensions():
    ring = geo.rectangle((72.0, 22.0), 10_000, 6_000)
    assert geo.area_km2(ring) == pytest.approx(60.0, rel=1e-3)


def test_area_is_right_at_high_latitude_too():
    """A projection-free area has to work away from the equator, not just near it."""
    ring = geo.rectangle((20.0, 68.0), 10_000, 6_000)
    assert geo.area_km2(ring) == pytest.approx(60.0, rel=2e-3)


def test_local_frame_round_trips():
    ring = geo.rectangle((69.7, 22.74), 4000, 3000)
    frame = geo.frame_for(ring)
    for point in [(69.71, 22.75), (69.68, 22.72)]:
        back = frame.to_lonlat(*frame.to_m(point))
        assert back[0] == pytest.approx(point[0], abs=1e-7)
        assert back[1] == pytest.approx(point[1], abs=1e-7)


def test_local_frame_metres_are_metres():
    frame = geo.LocalFrame((70.0, 23.0))
    east, north = frame.to_m((70.0, 23.0 + 1000 / 111_195))
    assert east == pytest.approx(0.0, abs=1e-6)
    assert north == pytest.approx(1000.0, rel=1e-3)


def test_validate_ring_catches_a_bowtie():
    problems = geo.validate_ring([(0, 0), (1, 1), (1, 0), (0, 1)])
    assert any("self-intersect" in p for p in problems)


def test_validate_ring_accepts_a_normal_aoi():
    assert geo.validate_ring(geo.rectangle((70.0, 23.0), 5000, 4000)) == []


def test_contains_and_haversine():
    ring = geo.rectangle((70.0, 23.0), 4000, 4000)
    assert geo.contains(ring, (70.0, 23.0))
    assert not geo.contains(ring, (71.0, 23.0))
    assert geo.haversine_m((70.0, 23.0), (70.0, 23.0)) == pytest.approx(0.0)


def test_geojson_round_trip_and_multipolygon():
    ring = geo.rectangle((70.0, 23.0), 3000, 2000)
    doc = {"type": "FeatureCollection",
           "features": [geo.ring_to_geojson(ring, {"name": "x"})]}
    [back] = geo.rings_from_geojson(doc)
    assert geo.area_km2(back) == pytest.approx(geo.area_km2(ring), rel=1e-9)

    multi = {"type": "MultiPolygon",
             "coordinates": [[[list(p) for p in geo.closed(ring)]],
                             [[list(p) for p in geo.closed(ring)]]]}
    assert len(geo.rings_from_geojson(multi)) == 2


# ---------------------------------------------------------------------------
# raster
# ---------------------------------------------------------------------------

def test_box_blur_preserves_a_constant_field():
    """The bug that made every scene drift dark: edge handling in the blur."""
    r = Raster(9, 7, 10.0, 0, 0, [0.37] * 63)
    for radius in (1, 2, 3):
        blurred = r.box_blur(radius)
        assert all(v == pytest.approx(0.37) for v in blurred.cells)


def test_box_blur_replicates_edges_rather_than_zero_filling():
    r = Raster(5, 1, 1.0, 0, 0, [1.0, 2.0, 3.0, 4.0, 5.0])
    out = r.box_blur(1).cells
    assert out[0] == pytest.approx((1 + 1 + 2) / 3)
    assert out[2] == pytest.approx((2 + 3 + 4) / 3)
    assert out[4] == pytest.approx((4 + 5 + 5) / 3)


def test_box_blur_mean_is_not_biased():
    r = Raster(20, 20, 1.0, 0, 0, [float(i % 7) for i in range(400)])
    assert r.box_blur(2).mean() == pytest.approx(r.mean(), rel=0.05)


def test_mad_ignores_a_few_large_outliers():
    """Why the noise floor uses MAD: changes must not raise the threshold."""
    cells = [0.5] * 400
    for i in range(0, 40):
        cells[i] = 9.0
    r = Raster(20, 20, 1.0, 0, 0, cells)
    assert r.mad() == pytest.approx(0.0, abs=1e-9)
    assert r.stdev() > 2.0


def test_moment_axes_recover_a_rotated_rectangle():
    """An axis-aligned bbox loses every diagonal ship; moments do not."""
    for angle in (0.0, 30.0, 45.0, 70.0):
        r = Raster(80, 80, 1.0, 0, 0)
        cx = cy = 40.0
        rad = math.radians(angle)
        for col in range(80):
            for row in range(80):
                dx, dy = col - cx, row - cy
                u = dx * math.cos(rad) + dy * math.sin(rad)
                v = -dx * math.sin(rad) + dy * math.cos(rad)
                if abs(u) <= 15 and abs(v) <= 3:
                    r.set(col, row, 1.0)
        [comp] = label_components(threshold(r, 0.5))
        major, minor, _ = comp.axes
        assert major == pytest.approx(30, rel=0.18)
        assert minor == pytest.approx(6, rel=0.35)
        assert comp.elongation > 3.0


def test_connected_components_are_four_connected():
    r = Raster(5, 5, 1.0, 0, 0)
    r.set(1, 1, 1.0)
    r.set(2, 2, 1.0)     # diagonal neighbour only
    comps = label_components(threshold(r, 0.5))
    assert len(comps) == 2


def test_boundary_cells_are_the_component_cells_that_touch_outside():
    r = Raster(20, 20, 1.0, 0, 0)
    for row in range(5, 15):
        for col in range(5, 15):
            r.set(col, row, 1.0)
    [comp] = label_components(threshold(r, 0.5))
    boundary = set(comp.boundary_cells)
    assert (5, 5) in boundary and (14, 14) in boundary   # corners
    assert (9, 9) not in boundary                        # interior
    assert (4, 4) not in boundary                        # outside entirely
    # A solid 10x10 square has an 8x8 interior, so 36 cells on its rim.
    assert len(boundary) == 10 * 10 - 8 * 8


def _reference_interior(comp, margin):
    """The straightforward O(81n) definition, kept as the thing to match."""
    own = {(c, r) for c, r in comp.cells}
    return sorted(
        (c, r) for c, r in comp.cells
        if all((c + dc, r + dr) in own
               for dc in range(-margin, margin + 1)
               for dr in range(-margin, margin + 1)))


def _ragged_components(seed: int):
    """Blobs with holes punched in them — where a rim shortcut goes wrong."""
    import random
    rng = random.Random(seed)
    r = Raster(40, 40, 10.0, 0, 0)
    for _ in range(rng.randint(1, 3)):
        c0, r0 = rng.randint(3, 30), rng.randint(3, 30)
        for col in range(c0, min(39, c0 + rng.randint(2, 9))):
            for row in range(r0, min(39, r0 + rng.randint(2, 9))):
                if rng.random() > 0.12:
                    r.set(col, row, 1.0)
    return label_components(threshold(r, 0.5))


def test_interior_matches_the_straightforward_definition():
    """The fast version grows from the inner boundary. It has to agree exactly.

    Growing from the cells just *outside* instead — the first attempt — shifts
    everything one step inward and drops the outermost shell, which is invisible
    on a solid square and wrong on anything ragged.
    """
    from terrashield.change import interior

    compared = 0
    for seed in range(12):
        for comp in _ragged_components(seed):
            compared += 1
            for margin in (2, 4):
                assert sorted(interior(comp, margin)) == \
                    _reference_interior(comp, margin), (seed, margin)
    assert compared > 20, "fixture produced too few components to conclude"


def test_the_ring_excludes_the_shell_next_to_the_component():
    """A ring that includes the adjacent cells is not an annulus.

    edge_drop compares contrast inside a component with contrast around it, and
    the cells immediately outside still carry most of the object's signal. An
    inner radius that does not actually exclude them measures the object
    against itself.
    """
    r = Raster(30, 30, 10.0, 0, 0)
    for col in range(12, 18):
        for row in range(12, 18):
            r.set(col, row, 1.0)
    [comp] = label_components(threshold(r, 0.5))
    own = {(c, r_) for c, r_ in comp.cells}
    ring = set(_ring_cells(comp, 30, 30, inner=2, outer=4))

    assert ring
    assert not (ring & own)
    for c, row in ring:
        nearest = min(max(abs(c - oc), abs(row - orow)) for oc, orow in own)
        assert 2 <= nearest <= 4, ((c, row), nearest)


def test_neighbourhood_helpers_scale_to_a_large_region():
    """O(81n) per cell made a reservoir drawdown take minutes. It should not."""
    import time
    from terrashield.change import interior
    from terrashield.detect import edge_drop

    r = Raster(300, 300, 10.0, 0, 0)
    for row in range(40, 260):
        for col in range(40, 260):
            r.set(col, row, 1.0)
    [comp] = label_components(threshold(r, 0.5))
    assert comp.pixel_count == 220 * 220

    started = time.monotonic()
    core = interior(comp, 4)
    drop = edge_drop(comp, r)
    elapsed = time.monotonic() - started

    # Eroding a solid 220-cell square by four leaves exactly 212 squared.
    assert len(core) == 212 * 212
    assert drop == pytest.approx(1.0)
    assert elapsed < 5.0, f"took {elapsed:.1f}s on one 48,400-cell component"


def test_opening_removes_speckle_and_keeps_shapes():
    m = Mask(20, 20, 1.0, 0, 0, [False] * 400)
    m.set(0, 0, True)
    m.set(17, 3, True)
    for col in range(5, 12):
        for row in range(5, 12):
            m.set(col, row, True)
    opened = m.opened(1)
    assert not opened.get(0, 0)
    assert not opened.get(17, 3)
    assert opened.get(8, 8)


def test_shifted_edge_extends_rather_than_zero_filling():
    r = Raster(6, 6, 1.0, 0, 0, [0.5] * 36)
    shifted = r.shifted(2, 0)
    assert all(v == pytest.approx(0.5) for v in shifted.cells)


def test_resample_changes_grid_but_keeps_footprint():
    r = Raster(60, 40, 10.0, 0, 0, [0.4] * 2400)
    out = r.resample(30.0)
    assert (out.width, out.height) == (20, 13)
    assert out.gsd_m == 30.0
    assert out.origin_e == r.origin_e


def test_difference_refuses_mismatched_grids():
    a = Raster(10, 10, 10.0, 0, 0)
    b = Raster(10, 10, 30.0, 0, 0)
    with pytest.raises(ValueError, match="ground sample distance"):
        a.difference(b)


def test_percentile_interpolates():
    assert percentile([1.0, 2.0, 3.0, 4.0], 50.0) == pytest.approx(2.5)
    assert percentile([], 50.0) == 0.0


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------

def _decode_png(blob: bytes) -> tuple[int, int, bytes]:
    assert blob[:8] == b"\x89PNG\r\n\x1a\n"
    pos, width, height, idat = 8, 0, 0, b""
    while pos < len(blob):
        length = struct.unpack(">I", blob[pos:pos + 4])[0]
        kind = blob[pos + 4:pos + 8]
        data = blob[pos + 8:pos + 8 + length]
        crc = struct.unpack(">I", blob[pos + 8 + length:pos + 12 + length])[0]
        assert crc == zlib.crc32(kind + data) & 0xFFFFFFFF, f"bad CRC on {kind}"
        if kind == b"IHDR":
            width, height, depth, colour = struct.unpack(">IIBB", data[:10])
            assert (depth, colour) == (8, 2)
        elif kind == b"IDAT":
            idat += data
        pos += 12 + length
    return width, height, zlib.decompress(idat)


def test_encode_png_is_a_valid_png():
    blob = encode_png(3, 2, bytes([255, 0, 0] * 6))
    width, height, raw = _decode_png(blob)
    assert (width, height) == (3, 2)
    assert len(raw) == height * (1 + width * 3)
    assert raw[0] == 0                     # filter byte
    assert raw[1:4] == bytes([255, 0, 0])


def test_encode_png_rejects_a_short_buffer():
    with pytest.raises(ValueError):
        encode_png(4, 4, b"\x00\x00\x00")


def test_render_png_stretches_without_clipping_the_scene():
    """One glint must not crush the rest of the scene to black."""
    r = Raster(16, 16, 10.0, 0, 0,
               [0.25 + 0.10 * ((i * 7) % 11) / 11 for i in range(256)])
    r.set(0, 0, 9.9)                       # one specular glint
    width, height, raw = _decode_png(render_png(r))
    assert (width, height) == (16, 16)
    row8 = raw[1 + 8 * (1 + 16 * 3):][:48]
    assert max(row8) > 120


def test_render_png_of_a_flat_scene_is_not_black():
    """A uniform scene and a failed download must not look the same."""
    r = Raster(8, 8, 10.0, 0, 0, [0.42] * 64)
    _, _, raw = _decode_png(render_png(r))
    pixels = [b for i, b in enumerate(raw) if i % (1 + 8 * 3) != 0]
    assert min(pixels) > 60
