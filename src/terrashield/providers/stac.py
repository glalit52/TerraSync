"""A STAC catalogue as a `catalog.Provider`.

One client, many vendors. What differs between Copernicus and Planet is an
endpoint, a collection name, an auth style and an asset key -- all of which
live in `presets.py` -- so the searching, the paging and the mapping onto
`Scene` are written once here.

The mapping is the interesting part. `Scene` carries the fields the engines
actually reason about: cloud fraction, sun elevation, off-nadir angle, relative
orbit. Those are not TerraShield inventions, they are STAC extension
properties (`eo:cloud_cover`, `view:sun_elevation`, `view:off_nadir`,
`sat:relative_orbit`), which is why the two line up almost exactly. Where a
vendor omits one, the field is filled with a value that is *conservative*
rather than flattering -- an unknown cloud fraction is not zero.

Reading pixels is a separate problem and deliberately a separate dependency.
See `fetch` for why.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from ..domain import Aoi, Constellation, Scene, Sensor
from ..geo import bbox, contains
from ..raster import Mask, Raster
from .credentials import Credentials
from .presets import CollectionSpec, ProviderPreset
from .transport import Transport, TransportError, UrllibTransport

#: A page size that is polite to the vendor and enough for a monitoring window.
PAGE_LIMIT = 100
#: Hard stop, so a mis-specified search cannot walk a whole archive.
MAX_PAGES = 20
#: A scene whose footprint covers less of the AOI than this is dropped: it
#: would be analysed over a sliver and leave most of the site unseen while
#: looking like a usable acquisition. Partial coverage above it is handled
#: by marking the uncovered cells obscured.
MIN_FOOTPRINT = 0.5
#: Items remembered from searches, so reading pixels does not re-query.
ITEM_CACHE = 2000


class ProviderUnavailable(Exception):
    """The vendor could not be reached, or refused the request."""


class PixelsUnavailable(Exception):
    """The catalogue works, but this build cannot decode the imagery."""


@dataclass
class StacProvider:
    """Search a STAC catalogue and present the results as `Scene`s."""

    preset: ProviderPreset
    credentials: Credentials
    transport: Transport = field(default_factory=UrllibTransport)
    timeout: float = 30.0
    _items: dict = field(default_factory=dict, repr=False)
    _footprint: dict = field(default_factory=dict, repr=False)

    # -- catalogue ---------------------------------------------------------

    def search(self, aoi: Aoi, start: date, end: date,
               constellations: tuple[Constellation, ...] = ()) -> list[Scene]:
        """Acquisitions over the AOI in the window.

        Asked for no particular mission, it searches only the collections this
        build can read pixels from: the pipeline picks today's scene from this
        list, and a scene it cannot read would fail the day rather than fall
        back to one it can. Named missions are searched regardless, because
        coverage planning wants to know what exists.
        """
        if constellations:
            specs = [s for c in constellations
                     if (s := self.preset.for_constellation(c))]
        else:
            specs = [s for s in self.preset.collections if s.pixels_readable]
        if not specs:
            return []

        scenes: list[Scene] = []
        for spec in specs:
            scenes.extend(self._best_per_day(
                self._search_collection(aoi, start, end, spec)))
        #: Chronological, because every consumer of a scene list either walks it
        #: in time order or pairs adjacent entries.
        scenes.sort(key=lambda s: (s.acquired_at, s.id))
        return scenes

    def _search_collection(self, aoi: Aoi, start: date, end: date,
                           spec: CollectionSpec) -> list[Scene]:
        min_lon, min_lat, max_lon, max_lat = bbox(aoi.boundary)
        body = {
            "collections": [spec.collection],
            "bbox": [min_lon, min_lat, max_lon, max_lat],
            "datetime": f"{start.isoformat()}T00:00:00Z/"
                        f"{end.isoformat()}T23:59:59Z",
            "limit": PAGE_LIMIT,
            **self.preset.extra_search,
        }

        out: list[Scene] = []
        url = f"{self.preset.stac_url.rstrip('/')}/search"
        for _ in range(MAX_PAGES):
            payload = self._post(url, body)
            features = payload.get("features") or []
            for item in features:
                scene = self._scene_from(item, aoi, spec)
                if scene is None:
                    continue
                cover = footprint_coverage(item, aoi)
                if cover is not None and cover < MIN_FOOTPRINT:
                    continue
                self._remember(scene.id, item, cover)
                out.append(scene)

            #: STAC paging is a `next` link carrying either a body to POST or a
            #: token to merge into the next request.
            nxt = _next_link(payload)
            if not nxt or not features:
                break
            url = nxt.get("href") or url
            body = {**body, **(nxt.get("body") or {})}
        return out

    def _post(self, url: str, body: dict) -> dict:
        headers = {"Content-Type": "application/json",
                   "Accept": "application/geo+json,application/json"}
        try:
            headers.update(self.credentials.headers())
            response = self.transport.request(
                "POST", url, headers=headers,
                body=json.dumps(body).encode(), timeout=self.timeout)
        except TransportError as e:
            raise ProviderUnavailable(
                f"{self.preset.title}: {e.message}"
                + (f" -- {e.body[:300]}" if e.body else "")) from e
        return response.json()

    # -- mapping -----------------------------------------------------------

    def _scene_from(self, item: dict, aoi: Aoi,
                    spec: CollectionSpec) -> Scene | None:
        props = item.get("properties") or {}
        when = _acquired_at(props, item)
        if when is None:
            #: No timestamp is not a scene. Everything downstream -- pairing,
            #: coverage, baselines -- is indexed by time.
            return None

        return Scene(
            id=str(item.get("id") or ""),
            aoi_id=aoi.id,
            constellation=spec.constellation,
            sensor=spec.sensor,
            acquired_at=when,
            gsd_m=float(props.get("gsd") or spec.gsd_m),
            cloud_pct=_cloud_pct(props, spec),
            off_nadir_deg=_number(props, ("view:off_nadir", "off_nadir"), 0.0),
            sun_elevation_deg=_number(
                props, ("view:sun_elevation", "sun_elevation"),
                _from_zenith(props)),
            orbit=_orbit(props),
            checksum=str(item.get("id") or ""),
        )

    def _remember(self, scene_id: str, item: dict, cover: float | None) -> None:
        self._items[scene_id] = item
        self._footprint[scene_id] = cover
        while len(self._items) > ITEM_CACHE:
            oldest = next(iter(self._items))
            self._items.pop(oldest, None)
            self._footprint.pop(oldest, None)

    def _best_per_day(self, scenes: list[Scene]) -> list[Scene]:
        """One scene per day per mission: the one that covers the AOI best.

        An AOI near a tile edge is in two tiles' footprints, and catalogues
        also list reprocessed duplicates of one acquisition. Analysing the
        sliver instead of the full tile is the failure this prevents.
        Scenes with no footprint in the item are kept as they are.
        """
        best: dict[date, Scene] = {}
        unknown: list[Scene] = []
        for scene in scenes:
            cover = self._footprint.get(scene.id)
            if cover is None:
                unknown.append(scene)
                continue
            day = scene.acquired_on
            held = best.get(day)
            if held is None or (cover, -scene.cloud_pct) > (
                    self._footprint.get(held.id) or 0.0, -held.cloud_pct):
                best[day] = scene
        return unknown + list(best.values())

    def item_for(self, scene: Scene) -> dict:
        """The STAC item behind a scene: remembered from search, else fetched.

        `Scene` deliberately carries only what the engines reason about, so
        the asset URLs live on the item. Fetching by id is a standard STAC
        route, which keeps a scene read days after its search reproducible.
        """
        item = self._items.get(scene.id)
        if item is not None:
            return item
        spec = self.preset.for_constellation(scene.constellation)
        if spec is None:
            raise ProviderUnavailable(
                f"{self.preset.title} has no collection for {scene.constellation.value}")
        url = (f"{self.preset.stac_url.rstrip('/')}/collections/"
               f"{spec.collection}/items/{scene.id}")
        headers = {"Accept": "application/geo+json,application/json"}
        try:
            headers.update(self.credentials.headers())
            response = self.transport.request("GET", url, headers=headers,
                                              timeout=self.timeout)
        except TransportError as e:
            raise ProviderUnavailable(
                f"{self.preset.title}: item {scene.id}: {e.message}") from e
        item = response.json()
        self._remember(scene.id, item, None)
        return item

    def asset_href(self, item: dict, keys: tuple[str, ...]) -> str:
        """The first asset key this item actually has, in preference order.

        Vendors disagree about naming for the same product -- `red` on Earth
        Search is `B04` on Copernicus -- so preference order beats assuming.
        """
        assets = item.get("assets") or {}
        for key in keys:
            asset = assets.get(key)
            if asset and asset.get("href"):
                return str(asset["href"])
        return ""

    # -- pixels ------------------------------------------------------------

    def fetch(self, aoi: Aoi, scene: Scene,
              gsd_m: float | None = None) -> Raster:
        """Read the pixels for this AOI.

        Deliberately gated behind an optional dependency. Reading a window out
        of a cloud-optimised GeoTIFF means HTTP range requests, TIFF tile
        tables, several compression codecs and a coordinate reprojection --
        which is GDAL's job, and reimplementing it unverified is how a pipeline
        gets silently wrong pixels rather than an error. Wrong pixels here look
        exactly like real change.

        So: `pip install "terrashield[imagery]"` brings rasterio, and the core
        stays dependency-free for the air-gapped install. Until then this says
        what is missing instead of guessing.
        """
        try:
            from .cog import read_window          # noqa: PLC0415
        except ImportError as e:
            raise PixelsUnavailable(
                "reading real imagery needs the optional imagery extra: "
                'pip install "terrashield[imagery]". The catalogue search '
                "works without it, so coverage and revisit planning are "
                f"available now. (missing: {e.name})") from e
        return read_window(self, aoi, scene, gsd_m)

    def masks(self, aoi: Aoi, scene: Scene, r: Raster):
        """The vendor's own quality bands, never re-derived from brightness.

        Deriving them is what reported a new photovoltaic block as eleven
        hectares of inundation on a desert energy site: panels are as dark as
        water in the visible bands, and only the scene-classification layer
        separates them.
        """
        from ..catalog import SceneMasks            # noqa: PLC0415
        try:
            from .cog import read_quality_masks     # noqa: PLC0415
        except ImportError:
            #: Empty masks, not invented ones. A caller that gets no cloud mask
            #: should behave as though nothing is known to be cloudy and rely
            #: on the scene-level cloud fraction, rather than trusting a guess.
            return SceneMasks(cloud=Mask.like(r), water=Mask.like(r))
        return read_quality_masks(self, aoi, scene, r)

    # -- diagnostics -------------------------------------------------------

    def health(self) -> dict:
        """Can this provider actually be reached and used, right now."""
        status = {"provider": self.preset.name, "title": self.preset.title,
                  "stac_url": self.preset.stac_url,
                  "credentials": self.credentials.describe()}
        if not self.credentials.complete:
            status["reachable"] = False
            status["detail"] = "credentials not set"
            return status
        try:
            response = self.transport.request(
                "GET", f"{self.preset.stac_url.rstrip('/')}/collections",
                headers=self.credentials.headers(), timeout=self.timeout)
            payload = response.json()
            names = {c.get("id") for c in payload.get("collections") or []}
            wanted = {s.collection for s in self.preset.collections}
            status["reachable"] = True
            status["collections_present"] = sorted(wanted & names)
            status["collections_missing"] = sorted(wanted - names)
        except Exception as e:                       # noqa: BLE001
            status["reachable"] = False
            status["detail"] = str(e)[:300]
        return status


# ---------------------------------------------------------------------------
# Property mapping
# ---------------------------------------------------------------------------

def footprint_coverage(item: dict, aoi: Aoi, samples: int = 9) -> float | None:
    """Fraction of the AOI inside the item's footprint, or None if unknown.

    Sampled on a grid over the AOI rather than computed exactly: the decision
    it feeds is "most of the site, or a sliver", and a 9 x 9 sample answers
    that without a geometry library.
    """
    geometry = item.get("geometry") or {}
    kind = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if kind == "Polygon":
        rings = [coords[0]] if coords else []
    elif kind == "MultiPolygon":
        rings = [poly[0] for poly in coords if poly]
    else:
        return None
    rings = [[(float(p[0]), float(p[1])) for p in ring] for ring in rings]
    if not rings:
        return None
    min_lon, min_lat, max_lon, max_lat = bbox(aoi.boundary)
    inside_aoi = covered = 0
    for i in range(samples):
        for j in range(samples):
            pt = (min_lon + (max_lon - min_lon) * (i + 0.5) / samples,
                  min_lat + (max_lat - min_lat) * (j + 0.5) / samples)
            if not contains(aoi.boundary, pt):
                continue
            inside_aoi += 1
            if any(contains(ring, pt) for ring in rings):
                covered += 1
    return covered / inside_aoi if inside_aoi else None


def _next_link(payload: dict) -> dict | None:
    for link in payload.get("links") or []:
        if link.get("rel") == "next":
            return link
    return None


def _acquired_at(props: dict, item: dict) -> datetime | None:
    for key in ("datetime", "start_datetime", "created"):
        raw = props.get(key) or item.get(key)
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _number(props: dict, keys: tuple[str, ...], default: float) -> float:
    for key in keys:
        raw = props.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return default


def _cloud_pct(props: dict, spec: CollectionSpec) -> float:
    """Cloud cover, or a conservative stand-in when the vendor does not say.

    SAR is unaffected by cloud, so zero is the truth there rather than an
    assumption. For an optical scene with no cloud property, the honest value
    is 'unknown', and the conservative reading of unknown is 100 -- a scene
    treated as cloudy is skipped, while a scene wrongly treated as clear is
    differenced and produces findings out of weather.
    """
    if spec.sensor is Sensor.SAR:
        return 0.0
    value = _number(props, ("eo:cloud_cover", "cloud_cover", "cloudCover"), -1.0)
    return 100.0 if value < 0 else max(0.0, min(100.0, value))


def _from_zenith(props: dict) -> float:
    """Sun elevation from a zenith angle, when that is what the vendor gives."""
    zenith = _number(props, ("view:sun_zenith", "sun_zenith"), -1.0)
    return 90.0 - zenith if zenith >= 0 else 45.0


def _orbit(props: dict) -> str:
    """Relative orbit, as a string, because it is an identity and not a number.

    It matters: differencing two SAR scenes from different ground tracks
    compares different viewing geometries and produces change everywhere. The
    pipeline refuses such pairs, and this is the field it refuses on.
    """
    for key in ("sat:relative_orbit", "relative_orbit", "sat:absolute_orbit",
                "landsat:wrs_path"):
        raw = props.get(key)
        if raw not in (None, ""):
            direction = str(props.get("sat:orbit_state", "")).strip().lower()
            suffix = f"-{direction[:3]}" if direction else ""
            return f"{raw}{suffix}"
    return ""
