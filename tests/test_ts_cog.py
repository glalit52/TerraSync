"""Reading real pixels: placement, values, quality bands and refusals.

Every test writes small GeoTIFFs to disk in a real map projection (UTM, as
Sentinel-2 and Landsat ship) with features at known coordinates, then reads
them through the same `StacProvider.fetch` / `.masks` the pipeline calls. No
network: the STAC items point at the local files.

Skipped without the `imagery` extra, which is the point of it being optional.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

rasterio = pytest.importorskip("rasterio")
np = pytest.importorskip("numpy")

from rasterio.transform import from_origin  # noqa: E402
from rasterio.warp import transform as warp_points  # noqa: E402

from terrashield import pipeline  # noqa: E402
from terrashield.domain import Constellation  # noqa: E402
from terrashield.geo import frame_for  # noqa: E402
from terrashield.providers import cog  # noqa: E402
from terrashield.providers.stac import (PixelsUnavailable,  # noqa: E402
                                        footprint_coverage)
from terrashield.sensors import _grid_for  # noqa: E402
from terrashield.catalog import extent_m  # noqa: E402

from test_ts_providers import AOI, FakeTransport, make, ok, s2_item  # noqa: E402

UTM42N = "EPSG:32642"
#: A bright target inside the AOI, as (lon, lat).
TARGET = (69.705, 22.845)


def utm(lon: float, lat: float) -> tuple[float, float]:
    xs, ys = warp_points("EPSG:4326", UTM42N, [lon], [lat])
    return xs[0], ys[0]


def write_tif(path, *, fill: int, dtype="uint16", nodata=0, target=None,
              target_value=None, east_fraction=1.0, blocks=()):
    """A 10 m UTM tile around the AOI, with optional features.

    `east_fraction` < 1 leaves the eastern part of the AOI uncovered (nodata),
    as at the edge of a tile. `blocks` are (lon, lat, half_size_m, value).
    """
    cx, cy = utm(69.70, 22.84)
    size = 600                         # 6 km at 10 m, around a 4 x 3.2 km AOI
    west, north = cx - 3000, cy + 3000
    data = np.full((size, size), fill, dtype=dtype)
    if east_fraction < 1.0:
        data[:, int(size * (0.5 + (east_fraction - 0.5) * 4000 / 6000)):] = nodata
    for lon, lat, half, value in ([(*target, 25, target_value)] if target else []) + list(blocks):
        x, y = utm(lon, lat)
        c, r = int((x - west) / 10), int((north - y) / 10)
        h = int(half / 10)
        data[r - h:r + h, c - h:c + h] = value
    with rasterio.open(path, "w", driver="GTiff", width=size, height=size, count=1,
                       dtype=dtype, crs=UTM42N, nodata=nodata,
                       transform=from_origin(west, north, 10, 10),
                       tiled=True, blockxsize=256, blockysize=256) as dst:
        dst.write(data, 1)
    return str(path)


def footprint(lon_span=(69.6, 69.8), lat_span=(22.74, 22.94)):
    (w, e), (s, n) = lon_span, lat_span
    return {"type": "Polygon",
            "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


def item_with(red, scl=None, *, item_id="S2A_TEST_20260401", when="2026-04-01T05:42:11Z",
              scale=0.0001, offset=0.0, geometry="default"):
    item = s2_item(item_id, when)
    item["assets"] = {"red": {"href": red,
                              "raster:bands": [{"scale": scale, "offset": offset}]}}
    if scl:
        item["assets"]["scl"] = {"href": scl}
    item["geometry"] = footprint() if geometry == "default" else geometry
    return item


def scene_for(provider):
    return provider.search(AOI, date(2026, 4, 1), date(2026, 4, 30),
                           (Constellation.SENTINEL_2,))[0]


@pytest.fixture(autouse=True)
def _fresh_cache():
    cog._READS = cog._Lru(8)
    yield


# ---------------------------------------------------------------------------
# The grid
# ---------------------------------------------------------------------------

def test_the_projection_is_the_engines_own_local_frame():
    """Not an approximation: every point lands where LocalFrame puts it."""
    frame = frame_for(AOI.boundary)
    crs = cog.local_crs(AOI)
    points = [(69.68, 22.83), (69.72, 22.85), TARGET, frame.origin]
    xs, ys = warp_points("EPSG:4326", crs, [p[0] for p in points],
                         [p[1] for p in points])
    for (lon, lat), x, y in zip(points, xs, ys):
        e, n = frame.to_m((lon, lat))
        assert x == pytest.approx(e, abs=0.01)
        assert y == pytest.approx(n, abs=0.01)


def test_the_grid_is_the_one_the_synthetic_estate_renders_on():
    g = cog.grid_for(AOI, 10.0)
    r = _grid_for(None, extent_m(AOI), 10.0, "optical")
    assert (g.width, g.height, g.min_e, g.max_n) == (r.width, r.height,
                                                    r.origin_e, r.origin_n)


# ---------------------------------------------------------------------------
# Pixels
# ---------------------------------------------------------------------------

def test_a_target_lands_on_the_cell_that_holds_its_coordinates(tmp_path):
    red = write_tif(tmp_path / "B04.tif", fill=1000, target=TARGET, target_value=5000)
    provider, _ = make("earth-search", ok({"features": [item_with(red)]}))
    r = provider.fetch(AOI, scene_for(provider))

    g = cog.grid_for(AOI, 10.0)
    assert (r.width, r.height, r.gsd_m) == (g.width, g.height, 10.0)
    assert r.band == "optical"
    col, row = r.to_cell(*frame_for(AOI.boundary).to_m(TARGET))
    assert r.get(col, row) == pytest.approx(0.5, abs=0.01)
    assert r.get(2, 2) == pytest.approx(0.1, abs=0.005)


def test_the_items_own_offset_is_applied(tmp_path):
    """Sentinel-2 baseline 04.00 moved the offset to -0.1; the item says so."""
    red = write_tif(tmp_path / "B04.tif", fill=2000)
    provider, _ = make("earth-search",
                       ok({"features": [item_with(red, offset=-0.1)]}))
    r = provider.fetch(AOI, scene_for(provider))
    assert r.median() == pytest.approx(0.1, abs=0.005)


def test_reflectance_is_clipped_to_the_engines_range(tmp_path):
    red = write_tif(tmp_path / "B04.tif", fill=15000)
    provider, _ = make("earth-search", ok({"features": [item_with(red)]}))
    assert provider.fetch(AOI, scene_for(provider)).max() == 1.0


def test_uncovered_cells_are_neutral_and_marked_obscured_not_dark(tmp_path):
    """The edge of a tile must not read as a new dark structure."""
    red = write_tif(tmp_path / "B04.tif", fill=3000, east_fraction=0.7)
    provider, _ = make("earth-search", ok({"features": [item_with(red)]}))
    scene = scene_for(provider)
    r = provider.fetch(AOI, scene)
    masks = provider.masks(AOI, scene, r)

    east = r.width - 2
    assert masks.cloud.get(east, r.height // 2) is True
    assert masks.cloud.get(1, r.height // 2) is False
    assert r.get(east, r.height // 2) == pytest.approx(0.3, abs=0.005)
    assert 0.15 < masks.cloud.fraction < 0.45


def test_scene_classification_gives_cloud_shadow_and_water(tmp_path):
    red = write_tif(tmp_path / "B04.tif", fill=1000)
    cloud_at, shadow_at, water_at = (69.690, 22.835), (69.700, 22.835), (69.710, 22.845)
    scl = write_tif(tmp_path / "SCL.tif", fill=4, dtype="uint8", blocks=[
        (*cloud_at, 200, 9), (*shadow_at, 200, 3), (*water_at, 200, 6)])
    provider, _ = make("earth-search", ok({"features": [item_with(red, scl)]}))
    scene = scene_for(provider)
    r = provider.fetch(AOI, scene)
    masks = provider.masks(AOI, scene, r)
    frame = frame_for(AOI.boundary)

    def at(mask, lonlat):
        return mask.get(*r.to_cell(*frame.to_m(lonlat)))

    assert at(masks.cloud, cloud_at) and at(masks.cloud, shadow_at)
    assert at(masks.water, water_at) and not at(masks.cloud, water_at)
    assert not at(masks.cloud, (69.72, 22.85)) and not at(masks.water, (69.72, 22.85))


def test_landsat_qa_bits_are_decoded(tmp_path):
    red = write_tif(tmp_path / "SR_B4.tif", fill=20000)
    cloudy, wet = (69.690, 22.835), (69.710, 22.845)
    qa = write_tif(tmp_path / "QA.tif", fill=1 << 6, nodata=1, blocks=[
        (*cloudy, 300, (1 << 3) | (1 << 6)), (*wet, 300, (1 << 7) | (1 << 6))])
    item = item_with(red, item_id="LC09_TEST", scale=2.75e-5, offset=-0.2)
    item["assets"] = {"red": item["assets"]["red"], "qa_pixel": {"href": qa}}
    provider, _ = make("planetary-computer", ok({"features": [item]}))
    scene = provider.search(AOI, date(2026, 4, 1), date(2026, 4, 30),
                            (Constellation.LANDSAT_9,))[0]
    r = provider.fetch(AOI, scene, gsd_m=30.0)
    masks = provider.masks(AOI, scene, r)
    frame = frame_for(AOI.boundary)
    assert r.median() == pytest.approx(20000 * 2.75e-5 - 0.2, abs=0.01)
    assert masks.cloud.get(*r.to_cell(*frame.to_m(cloudy)))
    assert masks.water.get(*r.to_cell(*frame.to_m(wet)))


def test_coarser_grids_average_rather_than_alias(tmp_path):
    """A checkerboard read at 3x the cell size is grey, not black or white."""
    cx, cy = utm(69.70, 22.84)
    data = (np.indices((600, 600)).sum(axis=0) % 2 * 4000 + 1000).astype("uint16")
    path = str(tmp_path / "check.tif")
    with rasterio.open(path, "w", driver="GTiff", width=600, height=600, count=1,
                       dtype="uint16", crs=UTM42N, nodata=0,
                       transform=from_origin(cx - 3000, cy + 3000, 10, 10)) as dst:
        dst.write(data, 1)
    provider, _ = make("earth-search", ok({"features": [item_with(path)]}))
    r = provider.fetch(AOI, scene_for(provider), gsd_m=30.0)
    assert r.stdev() < 0.03
    assert r.mean() == pytest.approx(0.3, abs=0.03)


def test_a_repeat_read_is_served_from_memory(tmp_path):
    red = write_tif(tmp_path / "B04.tif", fill=1000)
    provider, _ = make("earth-search", ok({"features": [item_with(red)]}))
    scene = scene_for(provider)
    first = provider.fetch(AOI, scene)
    (tmp_path / "B04.tif").unlink()
    second = provider.fetch(AOI, scene)
    assert second.cells == first.cells and second is not first


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

def test_sentinel1_grd_is_refused_with_the_alternative_named():
    item = s2_item("S1A_IW_GRDH_TEST", "2026-04-02T01:12:00Z")
    item["properties"].pop("eo:cloud_cover")
    provider, _ = make("earth-search", ok({"features": [item]}))
    scene = provider.search(AOI, date(2026, 4, 1), date(2026, 4, 30),
                            (Constellation.SENTINEL_1,))[0]
    with pytest.raises(PixelsUnavailable, match="RTC"):
        provider.fetch(AOI, scene)


def test_default_searches_skip_collections_that_cannot_be_read():
    """Earth Search's S1 GRD and requester-pays Landsat are left out; a live
    run picked a Landsat day before this and could not read it."""
    provider, transport = make("earth-search", ok({"features": []}))
    provider.search(AOI, date(2026, 4, 1), date(2026, 4, 30))
    asked = [c["body"]["collections"][0] for c in transport.calls]
    assert asked == ["sentinel-2-l2a"]

    pc, transport = make("planetary-computer", ok({"features": []}),
                         ok({"features": []}), ok({"features": []}))
    pc.search(AOI, date(2026, 4, 1), date(2026, 4, 30))
    asked = [c["body"]["collections"][0] for c in transport.calls]
    assert asked == ["sentinel-2-l2a", "sentinel-1-rtc", "landsat-c2-l2"]


def test_a_grid_too_large_for_the_engines_is_refused(tmp_path):
    red = write_tif(tmp_path / "B04.tif", fill=1000)
    provider, _ = make("earth-search", ok({"features": [item_with(red)]}))
    with pytest.raises(PixelsUnavailable, match="too large"):
        provider.fetch(AOI, scene_for(provider), gsd_m=1.0)


def test_requester_pays_s3_is_refused_and_https_alternates_are_used():
    provider, _ = make("earth-search")
    scene = None
    with pytest.raises(PixelsUnavailable, match="requester-pays"):
        cog.resolve_href(provider, scene_stub(), {"href": "s3://usgs-landsat/x.tif"},
                         "landsat-c2-l2")
    asset = {"href": "s3://bucket/x.tif",
             "alternate": {"https": {"href": "https://host/x.tif"}}}
    assert cog.resolve_href(provider, scene_stub(), asset, "c") == "https://host/x.tif"
    assert scene is None


def scene_stub():
    class S:
        id = "scene-1"
    return S()


def test_planetary_computer_urls_are_signed_and_the_token_reused():
    cog._SAS_TOKENS.clear()
    provider, transport = make("planetary-computer",
                               ok({"token": "se=2026&sig=abc"}))
    asset = {"href": "https://sentinel2l2a01.blob.core.windows.net/x/B04.tif"}
    first = cog.resolve_href(provider, scene_stub(), asset, "sentinel-2-l2a")
    second = cog.resolve_href(provider, scene_stub(), asset, "sentinel-2-l2a")
    assert first == second == asset["href"] + "?se=2026&sig=abc"
    assert len(transport.calls) == 1
    assert transport.calls[0]["url"].endswith("/token/sentinel-2-l2a")


# ---------------------------------------------------------------------------
# Items and footprints
# ---------------------------------------------------------------------------

def test_an_item_not_seen_in_search_is_fetched_by_id(tmp_path):
    red = write_tif(tmp_path / "B04.tif", fill=1000)
    item = item_with(red)
    searcher, _ = make("earth-search", ok({"features": [item]}))
    scene = scene_for(searcher)

    fresh, transport = make("earth-search", ok(item))
    r = fresh.fetch(AOI, scene)
    assert r.median() == pytest.approx(0.1, abs=0.005)
    assert transport.calls[0]["method"] == "GET"
    assert transport.calls[0]["url"].endswith(
        "/collections/sentinel-2-l2a/items/S2A_TEST_20260401")


def test_footprint_coverage():
    assert footprint_coverage({"geometry": footprint()}, AOI) == 1.0
    west_third = footprint(lon_span=(69.6, 69.69))
    assert footprint_coverage({"geometry": west_third}, AOI) < 0.5
    assert footprint_coverage({}, AOI) is None


def test_slivers_are_dropped_and_the_best_tile_of_a_day_is_kept(tmp_path):
    full = item_with("unused", item_id="full")
    sliver = item_with("unused", item_id="sliver",
                       geometry=footprint(lon_span=(69.6, 69.69)))
    partial = item_with("unused", item_id="partial",
                        geometry=footprint(lon_span=(69.6, 69.71)))
    provider, _ = make("earth-search", ok({"features": [partial, sliver, full]}))
    scenes = provider.search(AOI, date(2026, 4, 1), date(2026, 4, 30),
                             (Constellation.SENTINEL_2,))
    assert [s.id for s in scenes] == ["full"]


# ---------------------------------------------------------------------------
# The pipeline, on real-format imagery
# ---------------------------------------------------------------------------

def test_the_pipeline_runs_a_day_on_geotiffs_and_finds_a_new_structure(tmp_path):
    """Two acquisitions; a bright 50 m block appears between them."""
    from terrashield.domain import Role
    from terrashield.store import Store
    from terrashield.alerts import default_rules
    from terrashield.domain import Organization
    from terrashield.geo import centroid

    before = write_tif(tmp_path / "before.tif", fill=1500)
    after = write_tif(tmp_path / "after.tif", fill=1500, target=TARGET,
                      target_value=6000)
    items = [item_with(before, item_id="S2_BEFORE", when="2026-04-01T05:42:11Z"),
             item_with(after, item_id="S2_AFTER", when="2026-04-06T05:42:11Z")]
    #: The default search asks every readable collection: on Earth Search,
    #: Sentinel-2 alone.
    provider, _ = make("earth-search", ok({"features": items}))

    store = Store(str(tmp_path / "ts.db"), org_id=AOI.org_id, actor="test",
                  role=Role.ADMIN)
    store.put_org(Organization(id=AOI.org_id, name="Test", country="IN"))
    for rule in default_rules(AOI.org_id):
        store.put_rule(rule)
    store.put_aoi(AOI)

    result = pipeline.run_day(store, provider, AOI, date(2026, 4, 6))
    assert result.scene_used == "S2_AFTER"
    assert result.notes == [] or all("cloud" not in n for n in result.notes)
    changes = store.list_changes(AOI.id)
    frame = frame_for(AOI.boundary)
    assert changes, "a 50 m bright block between two scenes is a change"
    target_e, target_n = frame.to_m(TARGET)
    near = [c for c in changes
            if abs(frame.to_m(centroid(c.geometry))[0] - target_e) < 60
            and abs(frame.to_m(centroid(c.geometry))[1] - target_n) < 60]
    assert near, [(c.change_type, centroid(c.geometry)) for c in changes]
    store.close()
    json.dumps(result.to_dict(), default=str)
