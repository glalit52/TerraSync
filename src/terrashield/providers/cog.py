"""Reading real pixels: cloud-optimised GeoTIFFs onto the AOI's own grid.

This is the module `StacProvider.fetch` and `.masks` have always imported, and
the only place in the package that touches a real image. It needs the
`imagery` extra (rasterio, which brings GDAL and numpy); importing it without
that fails with an ImportError naming rasterio, which `fetch` turns into an
instruction rather than a crash.

Three decisions carry the weight here.

**The pixels land on exactly the grid the engines already use.** Every engine
works in the AOI's local metre frame (`geo.LocalFrame`), and that frame is not
an approximation of a map projection -- it *is* one: an equirectangular
projection with its true-scale latitude and origin at the AOI centroid,
`+proj=eqc +lat_ts=φ0 +lat_0=φ0 +lon_0=λ0` on the same sphere. So GDAL warps
each scene straight onto the grid `sensors.render` would have produced for the
synthetic estate -- same width, height, origin and cell size -- and nothing
downstream can tell a real scene from a modelled one by its shape. The warp is
GDAL's, not ours: reprojection written by hand is how a pipeline gets pixels
that are subtly in the wrong place, and a misplaced pixel looks exactly like
change.

**Quality comes from the vendor's own bands, never from brightness.**
Sentinel-2's scene classification layer and Landsat's QA_PIXEL say which cells
are cloud, shadow, water or missing. See `stac.StacProvider.masks` for the
failure that rule came from.

**Missing data is obscured, not dark.** A scene rarely covers an AOI exactly:
tiles end, swaths end. Cells with no data are filled with the scene's median
(so they put no edge into a difference image) *and* marked obscured in the
cloud mask, so detection and change treat them as unseen. Filling them with
zero instead would report the edge of a satellite tile as a new dark structure.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.transform import Affine
from rasterio.vrt import WarpedVRT

from ..catalog import SceneMasks, extent_m
from ..domain import Aoi, Scene, Sensor
from ..geo import EARTH_RADIUS_M, frame_for
from ..raster import Mask, Raster
from ..sensors import SENSORS
from .stac import PixelsUnavailable, ProviderUnavailable, StacProvider
from .transport import TransportError

#: The engines are pure Python over flat lists. A 1500 x 1500 grid is already
#: several seconds per operation; a 30 cm scene over a 36 km2 site would be
#: twenty thousand cells a side and would not finish. Refuse, and say how to
#: ask for something that will.
MAX_CELLS = 1500 * 1500

#: Sentinel-2 scene classification classes that hide the ground: no data,
#: saturated or defective, cloud shadow, cloud medium and high probability,
#: thin cirrus.
SCL_OBSCURED = (0, 1, 3, 8, 9, 10)
SCL_WATER = 6

#: Landsat Collection 2 QA_PIXEL bits.
QA_FILL = 1 << 0
QA_DILATED_CLOUD = 1 << 1
QA_CIRRUS = 1 << 2
QA_CLOUD = 1 << 3
QA_CLOUD_SHADOW = 1 << 4
QA_WATER = 1 << 7
QA_OBSCURED = QA_FILL | QA_DILATED_CLOUD | QA_CIRRUS | QA_CLOUD | QA_CLOUD_SHADOW

#: Backscatter is mapped from decibels onto 0..1 over this range, which spans
#: open water (~-25 dB) to built-up and metal (~0 dB) in gamma-nought.
SAR_DB_RANGE = (-25.0, 0.0)

#: GDAL settings for reading COGs over HTTP: no directory listings, ranged
#: reads merged, a few retries for a busy object store.
GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.TIF,.tiff,.TIFF",
    "GDAL_HTTP_MULTIRANGE": "YES",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "1",
    "VSI_CACHE": "TRUE",
}


# ---------------------------------------------------------------------------
# The grid
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Grid:
    """The AOI's local metre frame, at one cell size, as GDAL sees it."""

    width: int
    height: int
    gsd_m: float
    min_e: float
    max_n: float
    crs: CRS

    @property
    def transform(self) -> Affine:
        return Affine(self.gsd_m, 0.0, self.min_e, 0.0, -self.gsd_m, self.max_n)

    @property
    def cells(self) -> int:
        return self.width * self.height


def local_crs(aoi: Aoi) -> CRS:
    """`geo.LocalFrame` for this AOI, as a projection GDAL can warp into.

    LocalFrame maps (lon, lat) to
        east  = R cos(φ0) (λ - λ0)
        north = R (φ - φ0)
    which is the equirectangular projection with latitude of true scale and
    latitude of origin both φ0, on a sphere of radius R.
    """
    lon0, lat0 = frame_for(aoi.boundary).origin
    return CRS.from_proj4(
        f"+proj=eqc +lat_ts={lat0!r} +lat_0={lat0!r} +lon_0={lon0!r} "
        f"+x_0=0 +y_0=0 +R={EARTH_RADIUS_M!r} +units=m +no_defs")


def grid_for(aoi: Aoi, gsd_m: float) -> Grid:
    """The same grid `sensors.render` builds for the synthetic estate."""
    min_e, min_n, max_e, max_n = extent_m(aoi)
    width = max(8, int(round((max_e - min_e) / gsd_m)))
    height = max(8, int(round((max_n - min_n) / gsd_m)))
    return Grid(width, height, gsd_m, min_e, max_n, local_crs(aoi))


# ---------------------------------------------------------------------------
# Finding and opening the asset
# ---------------------------------------------------------------------------

_SAS_LOCK = threading.Lock()
_SAS_TOKENS: dict[str, tuple[str, float]] = {}
PC_SAS_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token/{collection}"


def _planetary_computer_token(provider: StacProvider, collection: str) -> str:
    """A shared-access token for one collection, cached until near expiry.

    Planetary Computer's search is open but its blobs are not: every asset URL
    has to carry a short-lived token from the /sas endpoint.
    """
    now = time.time()
    with _SAS_LOCK:
        cached = _SAS_TOKENS.get(collection)
        if cached and cached[1] > now + 300:
            return cached[0]
    try:
        response = provider.transport.request(
            "GET", PC_SAS_URL.format(collection=collection), timeout=provider.timeout)
    except TransportError as e:
        raise ProviderUnavailable(
            f"{provider.preset.title}: could not sign asset URLs: {e.message}") from e
    doc = response.json()
    token = str(doc.get("token") or "")
    if not token:
        raise ProviderUnavailable(f"{provider.preset.title}: no token in SAS response")
    #: Tokens last about an hour; the expiry is not always given, so assume
    #: the short end and refresh early rather than read a 403 mid-scene.
    with _SAS_LOCK:
        _SAS_TOKENS[collection] = (token, now + 45 * 60)
    return token


def resolve_href(provider: StacProvider, scene: Scene, asset: dict,
                 collection: str) -> str:
    """An address GDAL can open: https (signed if the vendor needs it) or a path."""
    href = str(asset.get("href") or "")
    if href.startswith("s3://"):
        #: Some vendors publish the s3:// form first and an https alternate.
        for alternate in (asset.get("alternate") or {}).values():
            alt = str((alternate or {}).get("href") or "")
            if alt.startswith("https://"):
                href = alt
                break
        else:
            raise PixelsUnavailable(
                f"{scene.id}: the asset is only published in an S3 bucket "
                f"({href.split('/')[2]}), which for this collection is "
                "requester-pays; reading it needs AWS credentials billed per "
                "request, which this build does not use")
    if provider.preset.signing == "planetary-computer" and href.startswith("https://"):
        token = _planetary_computer_token(provider, collection)
        href = f"{href}{'&' if '?' in href else '?'}{token}"
    return href


def _asset(provider: StacProvider, scene: Scene,
           keys: tuple[str, ...]) -> tuple[str, dict] | None:
    item = provider.item_for(scene)
    assets = item.get("assets") or {}
    for key in keys:
        asset = assets.get(key)
        if asset and asset.get("href"):
            spec = provider.preset.for_constellation(scene.constellation)
            collection = str(item.get("collection") or (spec.collection if spec else ""))
            return resolve_href(provider, scene, asset, collection), asset
    return None


def _gdal_env(provider: StacProvider) -> dict[str, str]:
    env = dict(GDAL_ENV)
    headers = provider.credentials.headers() if provider.credentials.complete else {}
    if headers:
        env["GDAL_HTTP_HEADERS"] = "\r\n".join(f"{k}: {v}" for k, v in headers.items())
    return env


def _scale_offset(asset: dict, default_scale: float,
                  default_offset: float) -> tuple[float, float]:
    """The vendor's own conversion from stored integers to physical values.

    Sentinel-2 changed its offset in processing baseline 04.00, so a fixed
    constant is wrong for half the archive; the item says which applies.
    """
    bands = asset.get("raster:bands") or []
    if bands and isinstance(bands[0], dict):
        b = bands[0]
        return (float(b.get("scale", default_scale)),
                float(b.get("offset", default_offset)))
    return default_scale, default_offset


def warp(href: str, grid: Grid, env: dict[str, str], *,
         categorical: bool = False) -> np.ma.MaskedArray:
    """Band 1 of `href`, warped onto `grid`. Cells with no data are masked."""
    with rasterio.Env(**env):
        try:
            src = rasterio.open(href)
        except rasterio.errors.RasterioIOError as e:
            raise ProviderUnavailable(f"could not open {href.split('?')[0]}: {e}") from e
        with src:
            if src.crs is None:
                #: An image with no georeferencing cannot be placed on the AOI.
                #: Sentinel-1 GRD is like this: geolocated by ground control
                #: points, not a projection, and not radiometrically calibrated.
                raise PixelsUnavailable(
                    f"{href.split('?')[0].rsplit('/', 1)[-1]} is not "
                    "map-projected, so it cannot be placed on the AOI grid; "
                    "use an analysis-ready product (e.g. Sentinel-1 RTC)")
            src_m = _pixel_size_m(src)
            downsampling = grid.gsd_m / src_m >= 1.5 if src_m else False
            if categorical:
                resampling = Resampling.mode if downsampling else Resampling.nearest
            else:
                resampling = Resampling.average if downsampling else Resampling.bilinear
            nodata = src.nodata if src.nodata is not None else 0
            with WarpedVRT(src, crs=grid.crs, transform=grid.transform,
                           width=grid.width, height=grid.height,
                           resampling=resampling, src_nodata=nodata,
                           nodata=nodata) as vrt:
                return vrt.read(1, masked=True)


def _pixel_size_m(src: Any) -> float:
    res = abs(src.res[0])
    if src.crs.is_geographic:
        return res * (np.pi * EARTH_RADIUS_M / 180.0)
    return res * (src.crs.linear_units_factor[1] if src.crs.linear_units_factor else 1.0)


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------

class _Lru:
    """Small and bounded: a day's analysis re-reads the same two scenes."""

    def __init__(self, size: int) -> None:
        self.size = size
        self._d: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                return self._d[key]
            return None

    def put(self, key, value) -> None:
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self.size:
                self._d.popitem(last=False)


#: (raster, no-data mask) together, so one can never outlive the other.
_READS = _Lru(8)


def _key(provider: StacProvider, scene: Scene, grid: Grid) -> tuple:
    return (provider.preset.name, scene.id, grid.gsd_m, grid.width, grid.height,
            grid.min_e, grid.max_n)


# ---------------------------------------------------------------------------
# The two functions StacProvider calls
# ---------------------------------------------------------------------------

def read_window(provider: StacProvider, aoi: Aoi, scene: Scene,
                gsd_m: float | None = None) -> Raster:
    """The scene's analysis band over the AOI, normalised 0..1, north up."""
    spec = provider.preset.for_constellation(scene.constellation)
    if spec is None:
        raise PixelsUnavailable(
            f"{provider.preset.title} has no collection for {scene.constellation.value}")
    if not spec.pixels_readable:
        raise PixelsUnavailable(f"{spec.collection} on {provider.preset.title}: "
                                f"{spec.unreadable_reason}")

    model = SENSORS.get(scene.constellation)
    gsd = gsd_m or (model.gsd_m if model else spec.gsd_m)
    grid = grid_for(aoi, gsd)
    if grid.cells > MAX_CELLS:
        raise PixelsUnavailable(
            f"{grid.width} x {grid.height} cells at {gsd} m is too large for the "
            f"engines (limit {MAX_CELLS:,}); request a coarser gsd_m or a "
            "smaller AOI")

    return _read(provider, scene, spec, grid)[0].copy()


def _read(provider: StacProvider, scene: Scene, spec, grid: Grid
          ) -> tuple[Raster, np.ndarray]:
    key = _key(provider, scene, grid)
    cached = _READS.get(key)
    if cached is not None:
        return cached

    found = _asset(provider, scene, spec.visible_assets)
    if found is None:
        raise PixelsUnavailable(
            f"{scene.id} has none of the assets {list(spec.visible_assets)}")
    href, asset = found
    data = warp(href, grid, _gdal_env(provider))

    if scene.sensor is Sensor.SAR:
        values = _normalise_sar(data, spec.value_kind)
        band = "sar"
    else:
        scale, offset = _scale_offset(asset, spec.value_scale, spec.value_offset)
        values = np.ma.clip(data.astype("float64") * scale + offset, 0.0, 1.0)
        band = "optical"

    nodata = np.ma.getmaskarray(values)
    valid = values.compressed()
    fill = float(np.median(valid)) if valid.size else 0.0
    cells = values.filled(fill).astype("float64").ravel().tolist()

    raster = Raster(grid.width, grid.height, grid.gsd_m, grid.min_e, grid.max_n,
                    cells, band=band)
    _READS.put(key, (raster, nodata))
    return raster, nodata


def _normalise_sar(data: np.ma.MaskedArray, value_kind: str) -> np.ma.MaskedArray:
    linear = data.astype("float64")
    if value_kind == "db":
        db = linear
    else:
        #: Zero and negative backscatter are no-data in practice (border noise
        #: removal), not infinitely dark ground.
        linear = np.ma.masked_less_equal(linear, 0.0)
        db = 10.0 * np.ma.log10(linear)
    lo, hi = SAR_DB_RANGE
    return np.ma.clip((db - lo) / (hi - lo), 0.0, 1.0)


def read_quality_masks(provider: StacProvider, aoi: Aoi, scene: Scene,
                       r: Raster) -> SceneMasks:
    """Cloud (meaning: anything hiding the ground) and water, from the vendor."""
    spec = provider.preset.for_constellation(scene.constellation)
    grid = grid_for(aoi, r.gsd_m)
    if (grid.width, grid.height) != (r.width, r.height):
        raise ValueError("raster is not on this AOI's grid; masks would not align")

    if spec is None or not spec.pixels_readable:
        return SceneMasks(cloud=Mask.like(r), water=Mask.like(r))
    _, nodata = _read(provider, scene, spec, grid)
    cloud = np.array(nodata, dtype=bool, copy=True).reshape(grid.height, grid.width)
    water = np.zeros_like(cloud)

    if spec is not None and spec.quality_kind and spec.quality_asset:
        found = _asset(provider, scene, spec.quality_asset)
        if found is not None:
            href, _ = found
            q = warp(href, grid, _gdal_env(provider), categorical=True)
            qv = np.asarray(q.filled(0)).astype("int64")
            if spec.quality_kind == "scl":
                cloud |= np.isin(qv, SCL_OBSCURED)
                water |= qv == SCL_WATER
            elif spec.quality_kind == "landsat_qa":
                cloud |= (qv & QA_OBSCURED) != 0
                water |= (qv & QA_WATER) != 0
    water &= ~cloud

    def to_mask(bits: np.ndarray) -> Mask:
        return Mask(grid.width, grid.height, r.gsd_m, r.origin_e, r.origin_n,
                    bits.ravel().tolist())

    return SceneMasks(cloud=to_mask(cloud), water=to_mask(water))
