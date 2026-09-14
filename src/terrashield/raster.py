"""A raster, and the image algebra change detection needs, in the standard library.

There is a real argument for rasterio and numpy, and a production deployment
should use them. But the whole value of this package is that the analysis is
inspectable end to end, and a change mask that arrives as an opaque array from
a C extension is exactly the black box PRD section 22 warns against. Everything
here is a flat list of floats and a loop you can read.

The cost is speed. A 600 x 600 grid at 10 m ground sample distance covers 36
km2 and takes a fraction of a second per operation, which is the right scale
for a monitored site; a national mosaic is not this module's job.

Convention: row 0 is the north edge, column 0 is the west edge, values are
normalised 0..1 -- surface reflectance for optical, normalised backscatter for
SAR. Cell coordinates are in the AOI's local metre frame (see geo.LocalFrame).
"""

from __future__ import annotations

import math
import struct
import zlib
from dataclasses import dataclass, field


@dataclass
class Raster:
    """A north-up grid of normalised values in a local metre frame."""

    width: int
    height: int
    gsd_m: float
    origin_e: float          # metres east of frame origin, west edge
    origin_n: float          # metres north of frame origin, north edge
    cells: list[float] = field(default_factory=list)
    band: str = "gray"

    def __post_init__(self) -> None:
        if not self.cells:
            self.cells = [0.0] * (self.width * self.height)
        elif len(self.cells) != self.width * self.height:
            raise ValueError(
                f"cells has {len(self.cells)} values, grid is "
                f"{self.width}x{self.height}={self.width * self.height}")

    # -- access ------------------------------------------------------------

    def get(self, col: int, row: int) -> float:
        return self.cells[row * self.width + col]

    def set(self, col: int, row: int, value: float) -> None:
        self.cells[row * self.width + col] = value

    def add(self, col: int, row: int, value: float) -> None:
        self.cells[row * self.width + col] += value

    def inside(self, col: int, row: int) -> bool:
        return 0 <= col < self.width and 0 <= row < self.height

    def like(self, fill: float = 0.0, band: str | None = None) -> "Raster":
        """An empty raster on the same grid."""
        return Raster(self.width, self.height, self.gsd_m, self.origin_e,
                      self.origin_n, [fill] * (self.width * self.height),
                      band or self.band)

    def copy(self) -> "Raster":
        return Raster(self.width, self.height, self.gsd_m, self.origin_e,
                      self.origin_n, list(self.cells), self.band)

    # -- coordinates -------------------------------------------------------

    def to_cell(self, east_m: float, north_m: float) -> tuple[int, int]:
        col = int((east_m - self.origin_e) / self.gsd_m)
        row = int((self.origin_n - north_m) / self.gsd_m)
        return col, row

    def to_m(self, col: int, row: int) -> tuple[float, float]:
        """Centre of the cell, in metres."""
        return (self.origin_e + (col + 0.5) * self.gsd_m,
                self.origin_n - (row + 0.5) * self.gsd_m)

    @property
    def cell_area_m2(self) -> float:
        return self.gsd_m * self.gsd_m

    # -- statistics --------------------------------------------------------

    def mean(self) -> float:
        return sum(self.cells) / len(self.cells)

    def median(self) -> float:
        return percentile(self.cells, 50.0)

    def mad(self) -> float:
        """Median absolute deviation. The noise floor estimator used throughout.

        Standard deviation over a difference image is dominated by the changes
        you are trying to find, so thresholding on it hides exactly the large
        events that matter. MAD is not.
        """
        med = self.median()
        return percentile([abs(v - med) for v in self.cells], 50.0)

    def stdev(self) -> float:
        m = self.mean()
        return math.sqrt(sum((v - m) ** 2 for v in self.cells) / max(len(self.cells) - 1, 1))

    def min(self) -> float:
        return min(self.cells)

    def max(self) -> float:
        return max(self.cells)

    # -- algebra -----------------------------------------------------------

    def difference(self, other: "Raster") -> "Raster":
        """Signed self - other. Positive means self is brighter."""
        self._require_same_grid(other)
        out = self.like(band="difference")
        out.cells = [a - b for a, b in zip(self.cells, other.cells)]
        return out

    def absolute(self) -> "Raster":
        out = self.like(band=self.band)
        out.cells = [abs(v) for v in self.cells]
        return out

    def scaled(self, gain: float, offset: float = 0.0) -> "Raster":
        out = self.like(band=self.band)
        out.cells = [v * gain + offset for v in self.cells]
        return out

    def clipped(self, lo: float = 0.0, hi: float = 1.0) -> "Raster":
        out = self.like(band=self.band)
        out.cells = [min(hi, max(lo, v)) for v in self.cells]
        return out

    def _require_same_grid(self, other: "Raster") -> None:
        if (self.width, self.height) != (other.width, other.height):
            raise ValueError("rasters are on different grids; resample first")
        if abs(self.gsd_m - other.gsd_m) > 1e-6:
            raise ValueError(
                f"ground sample distance differs ({self.gsd_m} vs {other.gsd_m}); "
                "resample to a common grid before differencing")

    # -- resampling and shifting -------------------------------------------

    def resample(self, gsd_m: float) -> "Raster":
        """Nearest-neighbour to a new ground sample distance, same footprint.

        Nearest rather than bilinear on purpose: a 30 m Landsat scene resampled
        to 10 m with smooth interpolation *looks* like 10 m data and invites
        conclusions the sensor cannot support. Blocky pixels keep the true
        resolution visible to the analyst.
        """
        if abs(gsd_m - self.gsd_m) < 1e-9:
            return self.copy()
        w = max(1, int(round(self.width * self.gsd_m / gsd_m)))
        h = max(1, int(round(self.height * self.gsd_m / gsd_m)))
        out = Raster(w, h, gsd_m, self.origin_e, self.origin_n, band=self.band)
        for row in range(h):
            src_row = min(self.height - 1, int(row * gsd_m / self.gsd_m))
            base = src_row * self.width
            for col in range(w):
                src_col = min(self.width - 1, int(col * gsd_m / self.gsd_m))
                out.cells[row * w + col] = self.cells[base + src_col]
        return out

    def crop(self, c0: int, r0: int, c1: int, r1: int) -> "Raster":
        """A sub-window, clamped to the grid, keeping its true georeference.

        Used for the detail view of a finding. The crop is not resampled: a
        small change stays a small number of pixels, and the interface scales
        it with nearest-neighbour so the analyst sees the resolution they
        actually have rather than a smooth picture implying one they do not.
        """
        c0 = max(0, min(self.width - 1, c0))
        r0 = max(0, min(self.height - 1, r0))
        c1 = max(c0 + 1, min(self.width, c1))
        r1 = max(r0 + 1, min(self.height, r1))
        out = Raster(c1 - c0, r1 - r0, self.gsd_m,
                     self.origin_e + c0 * self.gsd_m,
                     self.origin_n - r0 * self.gsd_m, band=self.band)
        for row in range(r0, r1):
            src = row * self.width
            dst = (row - r0) * out.width
            out.cells[dst:dst + out.width] = self.cells[src + c0:src + c1]
        return out

    def shifted(self, d_col: int, d_row: int) -> "Raster":
        """Translate by whole cells, edge-extending rather than zero-filling.

        Zero fill at the edges would create a bright false change along two
        borders of every co-registered pair, which is the single most common
        way a change detector produces confident nonsense.
        """
        out = self.like(band=self.band)
        for row in range(self.height):
            src_row = min(self.height - 1, max(0, row - d_row))
            for col in range(self.width):
                src_col = min(self.width - 1, max(0, col - d_col))
                out.cells[row * self.width + col] = self.cells[src_row * self.width + src_col]
        return out

    # -- neighbourhood -----------------------------------------------------

    def box_blur(self, radius: int = 1) -> "Raster":
        """Separable mean filter with edge replication. The background model.

        Prefix sums per row and per column rather than a sliding accumulator:
        a sliding sum has to add the cell entering the window and subtract the
        one leaving it, and at the edges -- where both indices clamp to the
        same cell -- it subtracts a value that never left. The error compounds
        along the row, so the whole image drifts dark. Prefix sums cannot get
        that wrong, and the cost is one extra array per row.
        """
        if radius <= 0:
            return self.copy()
        w, h, k = self.width, self.height, 2 * radius + 1
        tmp = self.like(band=self.band)
        for row in range(h):
            base = row * w
            pre = [0.0] * (w + 1)
            for i in range(w):
                pre[i + 1] = pre[i] + self.cells[base + i]
            first, last = self.cells[base], self.cells[base + w - 1]
            for col in range(w):
                a, b = col - radius, col + radius
                total = pre[min(w - 1, b) + 1] - pre[max(0, a)]
                if a < 0:
                    total += (-a) * first          # replicate the west edge
                if b > w - 1:
                    total += (b - w + 1) * last    # replicate the east edge
                tmp.cells[base + col] = total / k
        out = self.like(band=self.band)
        for col in range(w):
            pre = [0.0] * (h + 1)
            for i in range(h):
                pre[i + 1] = pre[i] + tmp.cells[i * w + col]
            first, last = tmp.cells[col], tmp.cells[(h - 1) * w + col]
            for row in range(h):
                a, b = row - radius, row + radius
                total = pre[min(h - 1, b) + 1] - pre[max(0, a)]
                if a < 0:
                    total += (-a) * first
                if b > h - 1:
                    total += (b - h + 1) * last
                out.cells[row * w + col] = total / k
        return out

    def local_contrast(self, radius: int = 3) -> "Raster":
        """self minus its local mean. Where objects live."""
        return self.difference(self.box_blur(radius))

    def texture(self, radius: int = 2) -> "Raster":
        """Local spread, as mean absolute deviation from the local mean.

        Separates a new building (bright, high texture at its edges) from a
        field that has simply been harvested (bright, flat).
        """
        bg = self.box_blur(radius)
        dev = self.like(band="texture")
        dev.cells = [abs(a - b) for a, b in zip(self.cells, bg.cells)]
        return dev.box_blur(radius)


# ---------------------------------------------------------------------------
# Masks and connected components
# ---------------------------------------------------------------------------

@dataclass
class Mask:
    """A boolean grid on the same footprint as the raster it came from."""

    width: int
    height: int
    gsd_m: float
    origin_e: float
    origin_n: float
    bits: list[bool] = field(default_factory=list)

    @classmethod
    def like(cls, r: Raster, value: bool = False) -> "Mask":
        return cls(r.width, r.height, r.gsd_m, r.origin_e, r.origin_n,
                   [value] * (r.width * r.height))

    def get(self, col: int, row: int) -> bool:
        return self.bits[row * self.width + col]

    def set(self, col: int, row: int, value: bool) -> None:
        self.bits[row * self.width + col] = value

    @property
    def count(self) -> int:
        return sum(1 for b in self.bits if b)

    @property
    def fraction(self) -> float:
        return self.count / len(self.bits)

    def to_m(self, col: int, row: int) -> tuple[float, float]:
        return (self.origin_e + (col + 0.5) * self.gsd_m,
                self.origin_n - (row + 0.5) * self.gsd_m)

    def erode(self, radius: int = 1) -> "Mask":
        out = Mask(self.width, self.height, self.gsd_m, self.origin_e,
                   self.origin_n, [False] * len(self.bits))
        for row in range(self.height):
            for col in range(self.width):
                if not self.get(col, row):
                    continue
                keep = True
                for dr in range(-radius, radius + 1):
                    for dc in range(-radius, radius + 1):
                        r2, c2 = row + dr, col + dc
                        if not (0 <= r2 < self.height and 0 <= c2 < self.width) \
                                or not self.get(c2, r2):
                            keep = False
                            break
                    if not keep:
                        break
                out.set(col, row, keep)
        return out

    def dilate(self, radius: int = 1) -> "Mask":
        out = Mask(self.width, self.height, self.gsd_m, self.origin_e,
                   self.origin_n, [False] * len(self.bits))
        for row in range(self.height):
            for col in range(self.width):
                if not self.get(col, row):
                    continue
                for dr in range(-radius, radius + 1):
                    for dc in range(-radius, radius + 1):
                        r2, c2 = row + dr, col + dc
                        if 0 <= r2 < self.height and 0 <= c2 < self.width:
                            out.set(c2, r2, True)
        return out

    def crop(self, c0: int, r0: int, c1: int, r1: int) -> "Mask":
        c0 = max(0, min(self.width - 1, c0))
        r0 = max(0, min(self.height - 1, r0))
        c1 = max(c0 + 1, min(self.width, c1))
        r1 = max(r0 + 1, min(self.height, r1))
        out = Mask(c1 - c0, r1 - r0, self.gsd_m,
                   self.origin_e + c0 * self.gsd_m,
                   self.origin_n - r0 * self.gsd_m,
                   [False] * ((c1 - c0) * (r1 - r0)))
        for row in range(r0, r1):
            for col in range(c0, c1):
                out.set(col - c0, row - r0, self.get(col, row))
        return out

    def opened(self, radius: int = 1) -> "Mask":
        """Erode then dilate: removes speckle, keeps shapes.

        SAR in particular produces single-cell false positives by the thousand.
        One opening pass is the difference between a change map and a starfield.
        """
        return self.erode(radius).dilate(radius)

    def closed(self, radius: int = 1) -> "Mask":
        """Dilate then erode: fills pinholes inside otherwise solid regions."""
        return self.dilate(radius).erode(radius)


@dataclass
class Component:
    """One connected region of a mask, measured."""

    label: int
    cells: list[tuple[int, int]]        # (col, row)
    gsd_m: float

    @property
    def pixel_count(self) -> int:
        return len(self.cells)

    @property
    def area_m2(self) -> float:
        return self.pixel_count * self.gsd_m * self.gsd_m

    @property
    def bbox_cells(self) -> tuple[int, int, int, int]:
        cols = [c for c, _ in self.cells]
        rows = [r for _, r in self.cells]
        return min(cols), min(rows), max(cols), max(rows)

    @property
    def centroid_cell(self) -> tuple[float, float]:
        n = len(self.cells)
        return sum(c for c, _ in self.cells) / n, sum(r for _, r in self.cells) / n

    @property
    def axes(self) -> tuple[float, float, float]:
        """(major_m, minor_m, orientation_deg) from the second moments.

        An axis-aligned bounding box is the wrong descriptor for anything that
        is not axis-aligned, and ships never are: a 240 x 32 m vessel lying at
        45 degrees has a nearly square bounding box, so a box-based elongation
        calls it compact and a box-based fill calls it sparse. Both are wrong,
        and together they lose every diagonal ship in the harbour.

        Image moments do not care about orientation. For a uniform rectangle of
        length L the variance along its axis is L^2/12, so sqrt(12 * eigenvalue)
        recovers the true side lengths whatever the heading.
        """
        n = self.pixel_count
        mx, my = self.centroid_cell
        sxx = syy = sxy = 0.0
        for c, r in self.cells:
            dx, dy = c - mx, r - my
            sxx += dx * dx
            syy += dy * dy
            sxy += dx * dy
        sxx /= n
        syy /= n
        sxy /= n
        #: A single cell has zero variance; give it the extent of one cell
        #: rather than dividing by zero downstream.
        common = (sxx + syy) / 2
        diff = math.sqrt(max(0.0, ((sxx - syy) / 2) ** 2 + sxy * sxy))
        lam1, lam2 = common + diff, max(0.0, common - diff)
        major = max(math.sqrt(12 * lam1), 1.0) * self.gsd_m
        minor = max(math.sqrt(12 * lam2), 1.0) * self.gsd_m
        angle = math.degrees(0.5 * math.atan2(2 * sxy, sxx - syy))
        return major, minor, angle

    @property
    def extent_m(self) -> float:
        """Longest ground dimension, orientation-independent."""
        return self.axes[0]

    @property
    def elongation(self) -> float:
        """Major axis over minor axis, at least 1.

        A road extension and a new warehouse have similar areas and completely
        different elongations, and that single number does most of the work of
        telling them apart.
        """
        major, minor, _ = self.axes
        return major / max(minor, 1e-6)

    @property
    def fill(self) -> float:
        """How solidly the component fills its own oriented extent, 0..1.

        Capped at 1: the moment-derived extent of a ragged shape can come out
        slightly smaller than the shape itself.
        """
        major, minor, _ = self.axes
        return min(1.0, self.area_m2 / max(major * minor, 1e-9))

    @property
    def boundary_cells(self) -> list[tuple[int, int]]:
        """Component cells that touch something outside it.

        The anchor for every neighbourhood measurement in the engines, and the
        reason they scale. Asking "are all 81 cells within four of this one
        also mine?" for every cell is O(81n), and at the whole-scene detection
        scale a component can be sixty thousand cells -- five million set
        lookups for one measurement, repeated per polarity, per scale, per
        scene. A reservoir drawdown brought a six-month run to a crawl in
        exactly this way.

        The *inner* boundary is the right anchor, and getting that wrong is
        subtle enough to be worth spelling out. For any cell outside the
        component, its nearest component cell is necessarily on this boundary
        -- if it were an interior cell, the straight path in would have entered
        the component sooner. So growing outward from here reaches exactly the
        same cells as growing from every cell, at a fraction of the cost.
        Growing from the cells just *outside* instead, as a first attempt did,
        silently shifts everything one step inward and drops the outermost
        shell, which biases every edge measurement that uses it.
        """
        own = {(c, r) for c, r in self.cells}
        return [(c, r) for c, r in self.cells
                if any((c + dc, r + dr) not in own
                       for dc, dr in ((-1, 0), (1, 0), (0, -1), (0, 1),
                                      (-1, -1), (1, 1), (-1, 1), (1, -1)))]

    @property
    def bbox_fill(self) -> float:
        """Fraction of the axis-aligned bounding box occupied. Kept for masks."""
        c0, r0, c1, r1 = self.bbox_cells
        return self.pixel_count / ((c1 - c0 + 1) * (r1 - r0 + 1))


def label_components(mask: Mask, min_cells: int = 1) -> list[Component]:
    """Four-connected flood fill, largest first.

    Four-connected rather than eight on purpose: eight-connectivity bridges
    diagonally touching regions, and at 10 m that merges a new building with
    the access track beside it into one unclassifiable blob.
    """
    seen = [False] * len(mask.bits)
    out: list[Component] = []
    label = 0
    for start in range(len(mask.bits)):
        if seen[start] or not mask.bits[start]:
            continue
        label += 1
        stack = [start]
        seen[start] = True
        cells: list[tuple[int, int]] = []
        while stack:
            idx = stack.pop()
            col, row = idx % mask.width, idx // mask.width
            cells.append((col, row))
            for c2, r2 in ((col - 1, row), (col + 1, row), (col, row - 1), (col, row + 1)):
                if 0 <= c2 < mask.width and 0 <= r2 < mask.height:
                    j = r2 * mask.width + c2
                    if mask.bits[j] and not seen[j]:
                        seen[j] = True
                        stack.append(j)
        if len(cells) >= min_cells:
            out.append(Component(label, cells, mask.gsd_m))
    out.sort(key=lambda c: c.pixel_count, reverse=True)
    return out


def threshold(r: Raster, level: float, absolute: bool = True) -> Mask:
    m = Mask.like(r)
    if absolute:
        m.bits = [abs(v) >= level for v in r.cells]
    else:
        m.bits = [v >= level for v in r.cells]
    return m


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile of an unsorted list."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = (pct / 100.0) * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

GRAY = [(v, v, v) for v in range(256)]

#: Amber on a dark ground. Chosen to stay legible when a change mask is
#: overlaid on grey imagery, and to survive being printed in a briefing pack.
HEAT = [
    (int(20 + 235 * (i / 255) ** 0.8),
     int(12 + 150 * (i / 255) ** 1.5),
     int(28 + 40 * (i / 255) ** 2.5))
    for i in range(256)
]


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def encode_png(width: int, height: int, rgb: bytes) -> bytes:
    """Minimal 8-bit RGB PNG. `rgb` is width*height*3 bytes, row-major.

    Thirty lines of zlib and struct, and it means the dashboard can show the
    actual change mask rather than a drawing of one. An intelligence product
    whose evidence images are mock-ups is not an intelligence product.
    """
    if len(rgb) != width * height * 3:
        raise ValueError("rgb buffer does not match the image dimensions")
    raw = bytearray()
    stride = width * 3
    for row in range(height):
        raw.append(0)                       # filter type 0 (none)
        raw += rgb[row * stride:(row + 1) * stride]
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _png_chunk(b"IEND", b""))


def _stretch(r: Raster, lo: float | None, hi: float | None) -> tuple[float, float]:
    """Display range for a scene: 2nd to 98th percentile, with a fallback.

    The percentile stretch keeps one specular glint from crushing the rest of
    the scene to black. But on a near-uniform image -- a calm reservoir, a
    fully overcast acquisition, a synthetic test field -- both percentiles land
    on the same value and the collapsed range renders every pixel black, which
    looks exactly like a failed download. Falling back to the full range when
    the percentiles collapse keeps a flat scene looking flat instead.
    """
    if lo is None:
        lo = percentile(r.cells, 2.0)
    if hi is None:
        hi = percentile(r.cells, 98.0)
    if hi - lo < 1e-6:
        lo, hi = r.min(), r.max()
    if hi - lo < 1e-6:
        lo, hi = lo - 0.5, hi + 0.5
    return lo, hi


def render_png(r: Raster, palette: list[tuple[int, int, int]] | None = None,
               lo: float | None = None, hi: float | None = None) -> bytes:
    """Stretch to the 2nd-98th percentile and encode.

    A linear stretch over the full range would let one specular glint flatten
    the rest of the scene to black, which is how a visually unreadable "before"
    panel ends up in a briefing.
    """
    palette = palette or GRAY
    lo, hi = _stretch(r, lo, hi)
    span = max(hi - lo, 1e-9)
    buf = bytearray()
    for v in r.cells:
        idx = int(255 * min(1.0, max(0.0, (v - lo) / span)))
        buf += bytes(palette[idx])
    return encode_png(r.width, r.height, bytes(buf))


def shared_stretch(*rasters: Raster) -> tuple[float, float]:
    """One display range covering several scenes.

    A before/after pair must be stretched together. Stretched apart, each panel
    is normalised to its own percentiles, so a sunnier acquisition is rendered
    back down to the same mid-grey and the analyst compares two images that
    have each been told to look average. Worse, the reverse happens too: a
    difference in display gain reads as a difference on the ground, which is
    the one thing a change viewer must never manufacture.
    """
    lows, highs = [], []
    for r in rasters:
        lo, hi = _stretch(r, None, None)
        lows.append(lo)
        highs.append(hi)
    return min(lows), max(highs)


def render_overlay_png(base: Raster, mask: Mask,
                       colour: tuple[int, int, int] = (255, 170, 40),
                       alpha: float = 0.55,
                       lo: float | None = None,
                       hi: float | None = None) -> bytes:
    """The after-scene with the change mask burned in. The analyst's main view."""
    lo, hi = _stretch(base, lo, hi)
    span = max(hi - lo, 1e-9)
    buf = bytearray()
    for i, v in enumerate(base.cells):
        g = int(255 * min(1.0, max(0.0, (v - lo) / span)))
        if mask.bits[i]:
            buf += bytes((int(g * (1 - alpha) + colour[0] * alpha),
                          int(g * (1 - alpha) + colour[1] * alpha),
                          int(g * (1 - alpha) + colour[2] * alpha)))
        else:
            buf += bytes((g, g, g))
    return encode_png(base.width, base.height, bytes(buf))
