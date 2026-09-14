"""The satellite data engine: what imagery exists, and how to get the pixels.

Two jobs, kept separate on purpose.

`search` answers "what could I have looked at" -- a catalogue query, the STAC
call in a real deployment. It is cheap, it is metadata only, and it is what the
timeline and the coverage report are built from, including the scenes that were
acquired and then thrown away for cloud. Coverage gaps are intelligence: a
border sector with no usable optical scene for nineteen days is a fact an
analyst needs on the screen, not an empty chart.

`fetch` returns pixels. Expensive, cached, and the only call that touches
imagery.

Revisit intervals, orbital phasing, sun angle and cloud climatology are modelled
rather than invented, because they determine what the product can promise. A
5-day Sentinel-2 revisit over a monsoon coast is not 73 looks a year, it is
closer to 40, and a change detector that needs a clean pair either waits or
switches to SAR. That constraint should show up in a demo, not in month three
of a pilot.

Swapping in real data means implementing `Provider` against Sentinel Hub,
Planet or Maxar. Nothing above this module knows which provider it has.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol

from .domain import Aoi, Constellation, Scene, Sensor
from .geo import frame_for
from .raster import Mask, Raster
from .sensors import SENSORS, cloud_mask, render, water_truth_mask
from .world import Rng, SiteTruth, seed_of


# ---------------------------------------------------------------------------
# Orbits
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OrbitSpec:
    """Enough orbital character to get revisit and overpass time right."""

    constellation: Constellation
    repeat_days: int              # effective revisit with the full constellation
    phase: int                    # orbital offset, so constellations do not co-observe
    local_overpass_h: float       # sun-synchronous descending node, local solar time
    tasked: bool = False          # commercial VHR only images when asked
    #: Days after which the *same* ground track recurs. Longer than
    #: `repeat_days` whenever a constellation gets its revisit from more than
    #: one satellite or more than one track. For Sentinel-1 this is the number
    #: that matters: interferometric and change work needs the same viewing
    #: geometry, which comes round every 12 days even though something passes
    #: overhead every 6.
    track_cycle_days: int = 1

    def track(self, when: date) -> str:
        n = (when.toordinal() + self.phase) % max(self.track_cycle_days, 1)
        return f"{'T' if self.tasked else 'R'}{n * 14 + 7:03d}"

    def observes_on(self, when: date) -> bool:
        if self.tasked:
            return False
        return (when.toordinal() + self.phase) % self.repeat_days == 0


ORBITS: dict[Constellation, OrbitSpec] = {
    # Sentinel-2A/B together give 5 days at the equator, 10:30 descending node.
    Constellation.SENTINEL_2: OrbitSpec(Constellation.SENTINEL_2, 5, 0, 10.5,
                                        track_cycle_days=10),
    # Sentinel-1: 12-day repeat per satellite; 6 days with both, 06:00 node.
    Constellation.SENTINEL_1: OrbitSpec(Constellation.SENTINEL_1, 6, 2, 6.0,
                                        track_cycle_days=12),
    # Landsat-9: 16-day repeat, 10:00 node.
    Constellation.LANDSAT_9: OrbitSpec(Constellation.LANDSAT_9, 16, 7, 10.0,
                                       track_cycle_days=16),
    # Tasked, so it never appears in a passive search; see `task`.
    Constellation.COMMERCIAL_VHR: OrbitSpec(
        Constellation.COMMERCIAL_VHR, 1, 0, 10.5, tasked=True),
}

DEFAULT_CONSTELLATIONS = (
    Constellation.SENTINEL_2, Constellation.SENTINEL_1, Constellation.LANDSAT_9,
)


# ---------------------------------------------------------------------------
# Illumination and weather
# ---------------------------------------------------------------------------

def solar_elevation(lat_deg: float, when: date, local_hour: float) -> float:
    """Solar elevation at a latitude, date and local solar hour, in degrees.

    Standard declination and hour-angle formula. It is here because sun angle
    is a first-order term in optical change detection: the same rooftop is 20%
    brighter in June than in December at 30 degrees north, which is larger than
    most of the changes worth finding.
    """
    doy = when.timetuple().tm_yday
    decl = math.radians(23.44 * math.sin(math.radians(360 / 365 * (doy - 81))))
    hour_angle = math.radians((local_hour - 12.0) * 15.0)
    lat = math.radians(lat_deg)
    sin_el = (math.sin(lat) * math.sin(decl)
              + math.cos(lat) * math.cos(decl) * math.cos(hour_angle))
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_el))))


@dataclass(frozen=True)
class CloudClimate:
    """Mean cloud cover by month, in percent. January first.

    Per-site because it has to be: a monsoon coast and a desert sector get
    completely different monitoring plans out of the same platform, and the
    plan is only credible if the weather is.
    """

    monthly_mean_pct: tuple[float, ...]
    variability: float = 22.0

    def sample(self, when: date, seed: int) -> float:
        mean = self.monthly_mean_pct[when.month - 1]
        rng = Rng(seed_of(seed, "cloud", when.isoformat()))
        #: Cloud is bimodal -- most days are clear or overcast, few are half --
        #: so a beta-ish draw beats a normal one here.
        u = rng.uniform()
        v = mean + self.variability * (u - 0.5) * 2 + rng.gauss(0, 8)
        if u > 0.72:
            v = max(v, mean + 25)
        return max(0.0, min(100.0, v))


ARID = CloudClimate((14, 12, 10, 9, 14, 46, 71, 68, 42, 12, 8, 10))
"""North-west India: dry nine months, monsoon overcast from June to September."""

COASTAL_MONSOON = CloudClimate((16, 14, 15, 18, 30, 68, 84, 82, 62, 26, 14, 13))
HIGH_ALTITUDE = CloudClimate((34, 38, 42, 44, 46, 40, 52, 50, 34, 22, 24, 30))


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

class Provider(Protocol):
    """The seam a real imagery vendor plugs into."""

    def search(self, aoi: Aoi, start: date, end: date,
               constellations: tuple[Constellation, ...]) -> list[Scene]: ...

    def fetch(self, aoi: Aoi, scene: Scene, gsd_m: float | None = None) -> Raster: ...

    def masks(self, aoi: Aoi, scene: Scene, r: Raster) -> "SceneMasks": ...


@dataclass
class SyntheticProvider:
    """Serves scenes from a modelled world, for demos, tests and development.

    It is a `Provider` like any other, which is the whole point: the pipeline
    cannot tell it apart from a vendor, so nothing above this line has to be
    rewritten when a customer's Sentinel Hub credentials arrive.
    """

    truths: dict[str, SiteTruth] = field(default_factory=dict)
    climates: dict[str, CloudClimate] = field(default_factory=dict)
    #: Rendered rasters are the expensive artefact and a day's analysis
    #: re-reads the same two scenes several times, so caching them matters.
    #:
    #: Bounded, though. An unbounded cache here is a slow leak with a
    #: monitoring workload: a six-month run over four sites touches four
    #: hundred scenes, each a 500 x 360 grid of floats, and the process was
    #: observed holding 1.9 GB by the third site. The working set is tiny --
    #: today's scene and the one being compared against -- so a small
    #: least-recently-used bound costs nothing and removes the failure mode
    #: where a long-running service dies on the estate that made it useful.
    cache_size: int = 16
    _cache: "OrderedDict[tuple[str, float], Raster]" = field(
        default_factory=OrderedDict, repr=False)
    _mask_cache: "OrderedDict[tuple[str, float], SceneMasks]" = field(
        default_factory=OrderedDict, repr=False)

    def _remember(self, cache: OrderedDict, key, value):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > max(self.cache_size, 2):
            cache.popitem(last=False)
        return value

    def _recall(self, cache: OrderedDict, key):
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
        return None

    def register(self, truth: SiteTruth, climate: CloudClimate = ARID) -> None:
        self.truths[truth.aoi_id] = truth
        self.climates[truth.aoi_id] = climate

    # -- catalogue ---------------------------------------------------------

    def search(self, aoi: Aoi, start: date, end: date,
               constellations: tuple[Constellation, ...] = DEFAULT_CONSTELLATIONS,
               ) -> list[Scene]:
        """Every acquisition over the AOI in the window, usable or not."""
        _, lat = aoi.centroid
        climate = self.climates.get(aoi.id, ARID)
        seed = seed_of(aoi.id, aoi.fingerprint)
        out: list[Scene] = []
        day = start
        while day <= end:
            for c in constellations:
                orbit = ORBITS[c]
                if not orbit.observes_on(day):
                    continue
                out.append(self._scene(aoi, c, orbit, day, lat, climate, seed))
            day += timedelta(days=1)
        out.sort(key=lambda s: (s.acquired_at, s.constellation.value))
        return out

    def task(self, aoi: Aoi, when: date,
             constellation: Constellation = Constellation.COMMERCIAL_VHR) -> Scene:
        """Order a tasked commercial acquisition for a specific day.

        Separate from `search` because it is a different commercial act: it
        costs money per square kilometre, it has to be requested before the
        overpass, and it is the only way to get the ground sample distance that
        vehicle-level pattern-of-life needs.
        """
        _, lat = aoi.centroid
        orbit = ORBITS[constellation]
        climate = self.climates.get(aoi.id, ARID)
        return self._scene(aoi, constellation, orbit, when, lat, climate,
                           seed_of(aoi.id, aoi.fingerprint), tasked=True)

    def _scene(self, aoi: Aoi, c: Constellation, orbit: OrbitSpec, day: date,
               lat: float, climate: CloudClimate, seed: int,
               tasked: bool = False) -> Scene:
        model = SENSORS[c]
        rng = Rng(seed_of(seed, c.value, day.isoformat()))
        cloud = 0.0 if model.sensor is Sensor.SAR else climate.sample(day, seed)
        sun_el = solar_elevation(lat, day, orbit.local_overpass_h)
        acquired = datetime.combine(
            day, time(hour=int(orbit.local_overpass_h),
                      minute=int((orbit.local_overpass_h % 1) * 60)),
            tzinfo=timezone.utc)
        scene_id = f"{c.value}:{aoi.id}:{day.isoformat()}"
        return Scene(
            id=scene_id,
            aoi_id=aoi.id,
            constellation=c,
            sensor=model.sensor,
            acquired_at=acquired,
            gsd_m=model.gsd_m,
            cloud_pct=round(cloud, 1),
            off_nadir_deg=round(abs(rng.gauss(0, 4.0)) if not tasked
                                else abs(rng.gauss(0, 12.0)), 1),
            sun_elevation_deg=round(sun_el, 1),
            orbit=orbit.track(day),
            checksum="",
        )

    # -- pixels ------------------------------------------------------------

    def fetch(self, aoi: Aoi, scene: Scene, gsd_m: float | None = None) -> Raster:
        truth = self.truths.get(aoi.id)
        if truth is None:
            raise KeyError(f"no imagery available for AOI {aoi.id}")
        gsd = gsd_m or SENSORS[scene.constellation].gsd_m
        key = (scene.id, gsd)
        cached = self._recall(self._cache, key)
        if cached is not None:
            return cached.copy()
        r = render(truth, scene, extent_m(aoi), gsd)
        self._remember(self._cache, key, r)
        return r.copy()

    def masks(self, aoi: Aoi, scene: Scene, r: Raster) -> SceneMasks:
        """Cloud and water bands for a fetched scene."""
        truth = self.truths.get(aoi.id)
        if truth is None:
            raise KeyError(f"no imagery available for AOI {aoi.id}")
        key = (scene.id + "|masks", r.gsd_m)
        cached = self._recall(self._mask_cache, key)
        if cached is not None:
            return cached
        return self._remember(
            self._mask_cache, key,
            SceneMasks(cloud=cloud_mask(r, scene),
                       water=water_truth_mask(truth, scene.acquired_on, r)))


@dataclass(frozen=True)
class SceneMasks:
    """The quality bands that ship alongside the pixels.

    Every serious imagery product has these -- Sentinel-2's scene
    classification layer, Landsat's QA_PIXEL, Planet's usable-data mask -- and
    a pipeline that re-derives them from brightness gets worse answers for no
    reason. They are modelled as a provider deliverable for that reason, not as
    something the engines are expected to invent.
    """

    cloud: Mask
    water: Mask


def extent_m(aoi: Aoi) -> tuple[float, float, float, float]:
    """The AOI's bounding box in its own local metre frame."""
    frame = frame_for(aoi.boundary)
    pts = frame.ring_to_m(aoi.boundary)
    es = [p[0] for p in pts]
    ns = [p[1] for p in pts]
    return (min(es), min(ns), max(es), max(ns))


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------

@dataclass
class Coverage:
    """What the catalogue actually delivered over a window. A reportable number.

    `longest_gap_days` is the one customers negotiate over, because it is the
    worst case answer to "how quickly would you have seen it".
    """

    aoi_id: str
    start: date
    end: date
    acquired: int
    usable: int
    by_constellation: dict[str, int]
    longest_gap_days: int
    last_usable: date | None
    rejected_reasons: dict[str, int]

    @property
    def usable_fraction(self) -> float:
        return self.usable / self.acquired if self.acquired else 0.0

    def to_dict(self) -> dict:
        return {
            "aoi_id": self.aoi_id,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "acquired": self.acquired,
            "usable": self.usable,
            "usable_fraction": round(self.usable_fraction, 3),
            "by_constellation": self.by_constellation,
            "longest_gap_days": self.longest_gap_days,
            "last_usable": self.last_usable.isoformat() if self.last_usable else None,
            "rejected_reasons": self.rejected_reasons,
        }


def coverage(aoi_id: str, scenes: list[Scene], start: date, end: date) -> Coverage:
    usable = [s for s in scenes if s.usable]
    by_c: dict[str, int] = {}
    for s in usable:
        by_c[s.constellation.value] = by_c.get(s.constellation.value, 0) + 1
    reasons: dict[str, int] = {}
    for s in scenes:
        if not s.usable:
            key = "cloud" if s.cloud_pct > Scene.CLOUD_LIMIT else "sun elevation"
            reasons[key] = reasons.get(key, 0) + 1

    days = sorted({s.acquired_on for s in usable})
    gap = 0
    prev = start
    for d in days:
        gap = max(gap, (d - prev).days)
        prev = d
    gap = max(gap, (end - prev).days)

    return Coverage(
        aoi_id=aoi_id, start=start, end=end,
        acquired=len(scenes), usable=len(usable), by_constellation=by_c,
        longest_gap_days=gap, last_usable=days[-1] if days else None,
        rejected_reasons=reasons,
    )


def best_pair(scenes: list[Scene], sensor: Sensor | None = None,
              min_separation_days: int = 5) -> tuple[Scene, Scene] | None:
    """The most recent usable pair that can legitimately be differenced.

    Same constellation on both sides is not fussiness. Differencing a
    Sentinel-2 scene against a Landsat one compares 10 m pixels with 30 m
    pixels through different spectral responses, and every field edge in the
    AOI lights up as change.

    For SAR, same ground track as well. Radar backscatter depends on the angle
    the surface is viewed from, so two passes from different tracks differ
    everywhere there is relief -- which over a border sector is everywhere.
    Pairing by track is why a 12-day SAR pair is worth more than a 6-day one.
    """
    pool = [s for s in scenes if s.usable and (sensor is None or s.sensor is sensor)]
    by_c: dict[tuple, list[Scene]] = {}
    for s in pool:
        key = (s.constellation, s.orbit if s.sensor is Sensor.SAR else "")
        by_c.setdefault(key, []).append(s)
    best: tuple[Scene, Scene] | None = None
    for group in by_c.values():
        group.sort(key=lambda s: s.acquired_at)
        for i in range(len(group) - 1, 0, -1):
            after = group[i]
            for j in range(i - 1, -1, -1):
                before = group[j]
                if (after.acquired_on - before.acquired_on).days >= min_separation_days:
                    if best is None or after.acquired_at > best[1].acquired_at:
                        best = (before, after)
                    break
            if best and best[1] is after:
                break
    return best
