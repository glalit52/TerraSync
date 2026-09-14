"""Geometry for areas of interest, without a geospatial stack.

An AOI is not a farm plot. It is a port, a border sector, a solar park -- tens
to thousands of square kilometres -- and the analyst who drew it will re-open
it in QGIS, so the numbers here have to agree with QGIS rather than merely be
self-consistent.

Two frames are used throughout and it is worth being explicit about them:

  * WGS84 (lon, lat) degrees, for anything that is stored, exchanged or drawn.
  * A local east/north metre frame anchored at an AOI's centroid, for anything
    that is measured or rastered. Over an AOI this approximation costs well
    under a pixel at 10 m ground sample distance, and it means the imaging and
    detection code can think in metres instead of in degrees-that-vary-by-
    latitude.

Everything is stdlib. The core has to run in an air-gapped facility where the
answer to "can you pip install GDAL" is no.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_RADIUS_M = 6_371_008.8  # IUGG mean radius

Point = tuple[float, float]     # (lon, lat) degrees
Ring = list[Point]              # exterior ring, may or may not be closed
BBox = tuple[float, float, float, float]  # (min_lon, min_lat, max_lon, max_lat)


# ---------------------------------------------------------------------------
# Rings
# ---------------------------------------------------------------------------

def closed(ring: Ring) -> Ring:
    """The ring with its first vertex repeated at the end."""
    if len(ring) >= 3 and ring[0] != ring[-1]:
        return [*ring, ring[0]]
    return list(ring)


def area_km2(ring: Ring) -> float:
    """Geodesic area of a WGS84 ring, in square kilometres.

    Spherical excess rather than a projection, so an AOI that straddles a UTM
    zone boundary -- which border sectors routinely do -- still gets the right
    answer.
    """
    pts = closed(ring)
    if len(pts) < 4:
        return 0.0
    total = 0.0
    for (lon1, lat1), (lon2, lat2) in zip(pts, pts[1:]):
        d_lon = math.radians(lon2 - lon1)
        total += d_lon * (2 + math.sin(math.radians(lat1)) + math.sin(math.radians(lat2)))
    area_m2 = abs(total * EARTH_RADIUS_M * EARTH_RADIUS_M / 2.0)
    return area_m2 / 1_000_000.0


def centroid(ring: Ring) -> Point:
    """Area-weighted centroid (lon, lat).

    Planar within the ring. Right for AOI-scale polygons, wrong for ones that
    span a continent; AOIs are AOI-scale by definition.
    """
    pts = closed(ring)
    if len(pts) < 4:
        n = max(len(ring), 1)
        return (sum(p[0] for p in ring) / n, sum(p[1] for p in ring) / n)
    a2 = cx = cy = 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        cross = x1 * y2 - x2 * y1
        a2 += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if a2 == 0:
        n = len(pts) - 1
        return (sum(p[0] for p in pts[:-1]) / n, sum(p[1] for p in pts[:-1]) / n)
    return (cx / (3 * a2), cy / (3 * a2))


def bbox(ring: Ring) -> BBox:
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    return (min(lons), min(lats), max(lons), max(lats))


def bbox_union(boxes: list[BBox]) -> BBox:
    return (
        min(b[0] for b in boxes), min(b[1] for b in boxes),
        max(b[2] for b in boxes), max(b[3] for b in boxes),
    )


def contains(ring: Ring, point: Point) -> bool:
    """Ray casting. Points exactly on an edge are not guaranteed either way."""
    lon, lat = point
    pts = closed(ring)
    inside = False
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        if (y1 > lat) != (y2 > lat):
            x_at = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < x_at:
                inside = not inside
    return inside


def bbox_overlaps(a: BBox, b: BBox) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def haversine_m(a: Point, b: Point) -> float:
    """Great-circle distance in metres."""
    lon1, lat1 = math.radians(a[0]), math.radians(a[1])
    lon2, lat2 = math.radians(b[0]), math.radians(b[1])
    dlon, dlat = lon2 - lon1, lat2 - lat1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def validate_ring(ring: Ring) -> list[str]:
    """Problems that would make an AOI unusable, found when it is drawn.

    An AOI with a bad ring does not fail loudly at draw time -- it fails
    quietly three weeks later as an empty change report, which is the worst
    possible failure mode for a monitoring system.
    """
    problems: list[str] = []
    if len(closed(ring)) < 4:
        problems.append("ring has fewer than three distinct vertices")
        return problems
    for lon, lat in ring:
        if not -180 <= lon <= 180 or not -90 <= lat <= 90:
            problems.append(f"vertex out of range: ({lon}, {lat})")
            break
    if area_km2(ring) <= 0:
        problems.append("ring encloses no area")
    if _self_intersects(closed(ring)):
        problems.append("ring self-intersects")
    min_lon, min_lat, max_lon, max_lat = bbox(ring)
    if max_lon - min_lon > 180:
        problems.append("ring spans more than 180 degrees of longitude; "
                        "split it at the antimeridian")
    return problems


def _segments_cross(p1, p2, p3, p4) -> bool:
    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return (v > 1e-15) - (v < -1e-15)
    o1, o2 = orient(p1, p2, p3), orient(p1, p2, p4)
    o3, o4 = orient(p3, p4, p1), orient(p3, p4, p2)
    return o1 != o2 and o3 != o4


def _self_intersects(pts: Ring) -> bool:
    edges = list(zip(pts, pts[1:]))
    n = len(edges)
    for i in range(n):
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue  # first and last edges share a vertex
            if _segments_cross(*edges[i], *edges[j]):
                return True
    return False


def rectangle(centre: Point, width_m: float, height_m: float) -> Ring:
    """An axis-aligned rectangle of the given ground dimensions about a point."""
    lon, lat = centre
    dlat = (height_m / 2) / (math.pi * EARTH_RADIUS_M / 180)
    dlon = (width_m / 2) / (math.pi * EARTH_RADIUS_M / 180 * math.cos(math.radians(lat)))
    return [
        (lon - dlon, lat - dlat), (lon + dlon, lat - dlat),
        (lon + dlon, lat + dlat), (lon - dlon, lat + dlat),
    ]


def circle(centre: Point, radius_m: float, vertices: int = 32) -> Ring:
    lon, lat = centre
    deg_per_m_lat = 180 / (math.pi * EARTH_RADIUS_M)
    deg_per_m_lon = deg_per_m_lat / max(math.cos(math.radians(lat)), 1e-6)
    return [
        (lon + radius_m * deg_per_m_lon * math.cos(2 * math.pi * i / vertices),
         lat + radius_m * deg_per_m_lat * math.sin(2 * math.pi * i / vertices))
        for i in range(vertices)
    ]


# ---------------------------------------------------------------------------
# Local metre frame
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LocalFrame:
    """An east/north metre frame anchored at `origin`.

    Everything the imaging and detection code does happens here. Conversion
    back to WGS84 happens once, at the boundary, when a detection or a change
    geometry is written out.
    """

    origin: Point

    @property
    def _deg_per_m_lat(self) -> float:
        return 180 / (math.pi * EARTH_RADIUS_M)

    @property
    def _deg_per_m_lon(self) -> float:
        return self._deg_per_m_lat / max(math.cos(math.radians(self.origin[1])), 1e-6)

    def to_m(self, point: Point) -> tuple[float, float]:
        """(lon, lat) -> (east_m, north_m)."""
        return (
            (point[0] - self.origin[0]) / self._deg_per_m_lon,
            (point[1] - self.origin[1]) / self._deg_per_m_lat,
        )

    def to_lonlat(self, east_m: float, north_m: float) -> Point:
        return (
            self.origin[0] + east_m * self._deg_per_m_lon,
            self.origin[1] + north_m * self._deg_per_m_lat,
        )

    def ring_to_m(self, ring: Ring) -> list[tuple[float, float]]:
        return [self.to_m(p) for p in ring]

    def ring_to_lonlat(self, ring_m: list[tuple[float, float]]) -> Ring:
        return [self.to_lonlat(e, n) for e, n in ring_m]


def frame_for(ring: Ring) -> LocalFrame:
    return LocalFrame(origin=centroid(ring))


# ---------------------------------------------------------------------------
# GeoJSON
# ---------------------------------------------------------------------------

def ring_to_geojson(ring: Ring, properties: dict | None = None) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[list(p) for p in closed(ring)]]},
        "properties": properties or {},
    }


def rings_from_geojson(doc: dict) -> list[Ring]:
    """Pull exterior rings out of a GeoJSON document.

    Accepts what analysts actually upload: a FeatureCollection, a bare Feature,
    a bare geometry, Polygon or MultiPolygon. Interior rings are dropped -- an
    AOI with a hole in it is a pair of AOIs as far as monitoring is concerned.
    """
    rings: list[Ring] = []

    def take_geometry(geom: dict) -> None:
        kind = geom.get("type")
        if kind == "Polygon":
            coords = geom.get("coordinates") or []
            if coords:
                rings.append([(float(x), float(y)) for x, y, *_ in coords[0]])
        elif kind == "MultiPolygon":
            for poly in geom.get("coordinates") or []:
                if poly:
                    rings.append([(float(x), float(y)) for x, y, *_ in poly[0]])
        elif kind == "GeometryCollection":
            for g in geom.get("geometries") or []:
                take_geometry(g)

    kind = doc.get("type")
    if kind == "FeatureCollection":
        for feat in doc.get("features") or []:
            if feat.get("geometry"):
                take_geometry(feat["geometry"])
    elif kind == "Feature":
        if doc.get("geometry"):
            take_geometry(doc["geometry"])
    elif kind:
        take_geometry(doc)
    return rings
