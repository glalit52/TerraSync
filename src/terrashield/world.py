"""Ground truth for the demo sites, and the terrain the sensors see.

This module exists because of a claim the rest of the package makes and has to
back up: that TerraShield *recovers* facts from pixels rather than reporting
facts it was handed. If the demo data were a list of pre-baked findings, the
detection and change engines would be decoration.

So the arrangement is deliberately adversarial. This module holds the truth --
which structures exist on which date, how many vehicles are parked where, when
construction starts -- and hands the imaging model nothing but a scene. The
pipeline never reads this module. Tests do, and they measure how much of the
truth the pipeline got back (tests/test_recovery.py). That number is the only
honest statement the product can make about detection quality before it has
touched a real customer's imagery.

Determinism matters as much as realism. `_rng` is a SplitMix64 written out
here rather than `random.Random`, so a scene rendered on a laptop in 2026 and
the same scene rendered in CI five years later are byte-identical, and a
regression in the detector cannot hide behind a reseeded world.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache

from .domain import AoiKind, ObjectClass


# ---------------------------------------------------------------------------
# Deterministic randomness
# ---------------------------------------------------------------------------

_MASK64 = (1 << 64) - 1


class Rng:
    """SplitMix64. Same stream on every platform and every Python version."""

    __slots__ = ("_s",)

    def __init__(self, seed: int) -> None:
        self._s = seed & _MASK64

    def next_u64(self) -> int:
        self._s = (self._s + 0x9E3779B97F4A7C15) & _MASK64
        z = self._s
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK64
        return z ^ (z >> 31)

    def uniform(self, lo: float = 0.0, hi: float = 1.0) -> float:
        return lo + (hi - lo) * (self.next_u64() >> 11) / float(1 << 53)

    def randint(self, lo: int, hi: int) -> int:
        """Inclusive on both ends."""
        return lo + int(self.next_u64() % max(1, hi - lo + 1))

    def gauss(self, mu: float = 0.0, sigma: float = 1.0) -> float:
        u1 = max(self.uniform(), 1e-12)
        u2 = self.uniform()
        return mu + sigma * math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2)

    def choice(self, items: list):
        return items[self.randint(0, len(items) - 1)]


def seed_of(*parts: object) -> int:
    """A stable 64-bit seed from any set of identifiers."""
    blob = "|".join(str(p) for p in parts).encode()
    return int.from_bytes(hashlib.blake2b(blob, digest_size=8).digest(), "big")


# ---------------------------------------------------------------------------
# Terrain
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1 << 19)
def _lattice(seed: int, ix: int, iy: int) -> float:
    """One noise lattice value.

    Cached because rendering a 5 km site at 10 m touches 200,000 cells and four
    octaves each, but only a few thousand distinct lattice points. Without the
    cache a scene render is three million hashes and takes the better part of a
    minute; with it, under a second.
    """
    return Rng(seed_of(seed, ix, iy)).uniform()


def _smooth(t: float) -> float:
    return t * t * (3 - 2 * t)


def value_noise(seed: int, x: float, y: float, scale: float) -> float:
    """Smoothed value noise in 0..1 at wavelength `scale`."""
    fx, fy = x / scale, y / scale
    ix, iy = math.floor(fx), math.floor(fy)
    tx, ty = _smooth(fx - ix), _smooth(fy - iy)
    a = _lattice(seed, ix, iy)
    b = _lattice(seed, ix + 1, iy)
    c = _lattice(seed, ix, iy + 1)
    d = _lattice(seed, ix + 1, iy + 1)
    return (a * (1 - tx) + b * tx) * (1 - ty) + (c * (1 - tx) + d * tx) * ty


def fbm(seed: int, x: float, y: float, scale: float, octaves: int = 4) -> float:
    """Fractal sum of value noise. Gives terrain its texture at every zoom."""
    total = amp = 0.0
    weight = 1.0
    for o in range(octaves):
        total += weight * value_noise(seed + o * 7919, x, y, scale / (2 ** o))
        amp += weight
        weight *= 0.5
    return total / amp


@dataclass(frozen=True)
class Terrain:
    """The unchanging backdrop: what the ground looks like with nothing on it.

    Water gets two separate mechanisms because real sites have two separate
    kinds. `shoreline` is a coast or a reservoir edge -- one coherent body with
    a wandering boundary, which is what Mundra and Sardar Sarovar have and what
    an inundation measurement is taken against. `water_level` is scattered
    seasonal ponding, which is what a salt flat has. Using the scattered model
    for a coastline produces an archipelago, and a change detector run over an
    archipelago that was supposed to be a harbour reports nothing useful.
    """

    seed: int
    base_reflectance: float          # optical, 0..1
    roughness: float                 # how much fbm modulates it
    feature_scale_m: float           # wavelength of the dominant land pattern
    water_level: float = -1.0        # fbm value below which the surface ponds
    sar_base: float = 0.28           # normalised backscatter of bare ground
    #: ("north"|"south"|"east"|"west", metres): water lies on that side of the
    #: line, with a natural edge. None for a site with no standing water body.
    shoreline: tuple[str, float] | None = None
    shoreline_wobble_m: float = 140.0

    def is_water(self, east_m: float, north_m: float) -> bool:
        if self.shoreline is not None:
            side, at = self.shoreline
            wobble = self.shoreline_wobble_m * (
                fbm(self.seed + 31, east_m, north_m, 1100.0) - 0.5) * 2
            if side == "north":
                return north_m > at + wobble
            if side == "south":
                return north_m < at + wobble
            if side == "east":
                return east_m > at + wobble
            if side == "west":
                return east_m < at + wobble
        if self.water_level > -1.0:
            return fbm(self.seed, east_m, north_m, self.feature_scale_m) < self.water_level
        return False

    def optical(self, east_m: float, north_m: float) -> float:
        if self.is_water(east_m, north_m):
            return 0.05                       # water is dark and flat in optical
        n = fbm(self.seed, east_m, north_m, self.feature_scale_m)
        return min(1.0, max(0.0, self.base_reflectance + self.roughness * (n - 0.5) * 2))

    def sar(self, east_m: float, north_m: float) -> float:
        if self.is_water(east_m, north_m):
            return 0.03                       # specular: water is the darkest thing in SAR
        n = fbm(self.seed, east_m, north_m, self.feature_scale_m)
        return min(1.0, max(0.0, self.sar_base + 0.22 * (n - 0.5) * 2))


# ---------------------------------------------------------------------------
# Things on the ground
# ---------------------------------------------------------------------------

@dataclass
class Feature:
    """A persistent structure. Exists between `appears_on` and `removed_on`."""

    id: str
    object_class: ObjectClass
    east_m: float
    north_m: float
    length_m: float
    width_m: float
    heading_deg: float = 0.0
    reflectance: float = 0.62         # optical brightness of the material
    backscatter: float = 0.75         # SAR: metal and concrete are bright
    appears_on: date | None = None
    removed_on: date | None = None

    def present_on(self, when: date) -> bool:
        if self.appears_on and when < self.appears_on:
            return False
        if self.removed_on and when >= self.removed_on:
            return False
        return True

    @property
    def footprint_m2(self) -> float:
        return self.length_m * self.width_m


@dataclass
class Zone:
    """Where transient objects of a given class can be, in metres."""

    name: str
    east_m: float
    north_m: float
    length_m: float
    width_m: float
    classes: tuple[ObjectClass, ...] = ()
    surface_reflectance: float | None = None   # apron/road/yard, if it differs
    surface_backscatter: float | None = None

    def place(self, rng: Rng) -> tuple[float, float]:
        return (self.east_m + rng.uniform(-0.5, 0.5) * self.length_m,
                self.north_m + rng.uniform(-0.5, 0.5) * self.width_m)


@dataclass
class ActivityProfile:
    """How many transient objects of a class are normally present.

    `weekly` is the real reason pattern-of-life beats a fixed threshold: a port
    with 40 trucks on Tuesday and 9 on Sunday is not anomalous on Tuesday, and
    a system without a weekday term will say it is, once a week, forever.
    """

    object_class: ObjectClass
    base_count: float
    weekly: tuple[float, ...] = (1.0,) * 7      # multiplier by weekday, Mon=0
    noise_sd: float = 0.12                      # proportional day-to-day variation
    zone: str = ""

    def expected(self, when: date) -> float:
        return self.base_count * self.weekly[when.weekday()]

    def count_on(self, when: date, seed: int) -> int:
        rng = Rng(seed_of(seed, self.object_class.value, when.isoformat()))
        v = self.expected(when) * (1.0 + rng.gauss(0.0, self.noise_sd))
        return max(0, int(round(v)))


@dataclass
class Episode:
    """A scripted period where the site departs from its own normal.

    Episodes are the ground truth for the anomaly engine. A test can assert
    that the score rises inside the window and falls outside it, which is a far
    stronger statement than asserting the score is some number.
    """

    id: str
    label: str
    start: date
    end: date
    count_multipliers: dict[ObjectClass, float] = field(default_factory=dict)
    #: Structures whose construction this episode explains, by feature id.
    feature_ids: tuple[str, ...] = ()

    def covers(self, when: date) -> bool:
        return self.start <= when <= self.end

    def multiplier(self, klass: ObjectClass) -> float:
        return self.count_multipliers.get(klass, 1.0)


@dataclass
class SiteTruth:
    """Everything true about one site. The pipeline is never given this."""

    aoi_id: str
    kind: AoiKind
    terrain: Terrain
    features: list[Feature] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)
    activity: list[ActivityProfile] = field(default_factory=list)
    episodes: list[Episode] = field(default_factory=list)
    seed: int = 0

    def zone(self, name: str) -> Zone | None:
        for z in self.zones:
            if z.name == name:
                return z
        return None

    def features_on(self, when: date) -> list[Feature]:
        return [f for f in self.features if f.present_on(when)]

    def episode_on(self, when: date) -> Episode | None:
        for ep in self.episodes:
            if ep.covers(when):
                return ep
        return None

    def counts_on(self, when: date) -> dict[ObjectClass, int]:
        """How many transient objects of each class are actually there."""
        ep = self.episode_on(when)
        out: dict[ObjectClass, int] = {}
        for prof in self.activity:
            n = prof.count_on(when, self.seed)
            if ep:
                n = int(round(n * ep.multiplier(prof.object_class)))
            out[prof.object_class] = out.get(prof.object_class, 0) + n
        return out

    def transients_on(self, when: date) -> list[Feature]:
        """Where each transient object actually sits on this date.

        Positions are re-drawn per date from the date's own seed: vehicles move
        overnight. That is what makes a difference image over a car park noisy
        in a way a real one is noisy, and it is why the change engine has to
        classify by shape and persistence rather than by "something differed".
        """
        out: list[Feature] = []
        counts = self.counts_on(when)
        for prof in self.activity:
            zone = self.zone(prof.zone)
            if zone is None:
                continue
            n = counts.get(prof.object_class, 0)
            spec = OBJECT_SPECS[prof.object_class]
            for i in range(n):
                rng = Rng(seed_of(self.seed, prof.object_class.value, when.isoformat(), i))
                east, north = zone.place(rng)
                out.append(Feature(
                    id=f"{prof.object_class.value}-{when.isoformat()}-{i}",
                    object_class=prof.object_class,
                    east_m=east, north_m=north,
                    length_m=spec[0] * rng.uniform(0.85, 1.15),
                    width_m=spec[1] * rng.uniform(0.85, 1.15),
                    heading_deg=rng.uniform(0, 180),
                    reflectance=spec[2] * rng.uniform(0.85, 1.15),
                    backscatter=spec[3] * rng.uniform(0.9, 1.1),
                ))
        return out


#: (length_m, width_m, optical reflectance, SAR backscatter) per transient class.
#: Dimensions are real: a shipping-container truck is ~16 m, a narrow-body
#: airliner ~38 m, a Panamax container ship ~290 m. This matters because the
#: detector classifies partly on extent, and a made-up size table would make the
#: recovery tests meaningless.
OBJECT_SPECS: dict[ObjectClass, tuple[float, float, float, float]] = {
    ObjectClass.VEHICLE: (4.5, 1.9, 0.72, 0.80),
    ObjectClass.TRUCK: (16.0, 2.6, 0.70, 0.85),
    ObjectClass.CONSTRUCTION_VEHICLE: (9.0, 3.2, 0.74, 0.88),
    ObjectClass.AIRCRAFT: (38.0, 34.0, 0.80, 0.90),
    ObjectClass.HELICOPTER: (16.0, 14.0, 0.76, 0.86),
    ObjectClass.VESSEL: (240.0, 32.0, 0.66, 0.92),
    ObjectClass.SMALL_BOAT: (14.0, 4.0, 0.68, 0.70),
    ObjectClass.CONTAINER_STACK: (24.0, 12.0, 0.60, 0.88),
}


def height_of(f: Feature) -> float:
    """Structure height in metres, for shadow casting.

    Stored as a derived property rather than a field so that a feature table
    loaded from a real vector source -- OSM building footprints, say -- does not
    have to carry a height it usually does not have.
    """
    if f.object_class in (ObjectClass.TOWER,):
        return 55.0
    if f.object_class in (ObjectClass.STORAGE_TANK,):
        return 18.0
    if f.object_class is ObjectClass.BUILDING:
        return max(4.0, min(24.0, 0.45 * min(f.length_m, f.width_m)))
    if f.object_class is ObjectClass.VESSEL:
        return 22.0
    if f.object_class is ObjectClass.AIRCRAFT:
        return 11.0
    return 0.0
