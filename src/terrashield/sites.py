"""The demo estate: four real places, monitored the way a customer would.

Every AOI here is civilian infrastructure that appears on public maps, and
that is a deliberate product decision rather than caution about the demo data.
PRD section 52 says not to open with a defence ministry, and it is right: ports,
solar parks and dams are where the dual-use market actually is, they buy on a
normal procurement cycle, and the change-detection problem is identical. A demo
built on somebody's air base makes the first sales conversation about the demo
instead of about the product.

The four are chosen to cover the three demonstrations in PRD section 53 plus
the disaster case:

  IN-MUN-PORT   Mundra, Gujarat      maritime activity and terminal expansion
  IN-BHD-SOLAR  Bhadla, Rajasthan    construction progress on critical energy
  IN-KCH-SECTOR Rann of Kutch        remote-area monitoring, sparse background
  IN-SSD-DAM    Sardar Sarovar       reservoir extent and inundation

Each carries a scripted episode -- a vessel surge, a construction push, a
vehicle concentration, a drawdown -- with known dates. Those dates are the
ground truth the anomaly engine is scored against in tests/test_recovery.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from . import geo
from .catalog import ARID, COASTAL_MONSOON, CloudClimate
from .domain import Aoi, AoiKind, ObjectClass
from .world import (
    ActivityProfile, Episode, Feature, SiteTruth, Terrain, Zone, seed_of,
)

DEMO_ORG = "org-sensegrass-demo"


@dataclass
class DemoSite:
    """An AOI, its ground truth and its weather, packaged together."""

    aoi: Aoi
    truth: SiteTruth
    climate: CloudClimate
    headline: str
    episode_label: str = ""

    @property
    def id(self) -> str:
        return self.aoi.id


def _aoi(aoi_id: str, name: str, centre: tuple[float, float],
         width_m: float, height_m: float, kind: AoiKind, desc: str) -> Aoi:
    return Aoi(
        id=aoi_id, org_id=DEMO_ORG, name=name,
        boundary=geo.rectangle(centre, width_m, height_m),
        kind=kind, country="IN", description=desc,
    )


# ---------------------------------------------------------------------------
# Mundra: maritime domain awareness
# ---------------------------------------------------------------------------

def mundra() -> DemoSite:
    """India's largest commercial port. Vessels, container yards, a new berth.

    The maritime demo. The interesting property is that the berth and the
    warehouse are permanent changes worth an alert, while the vessel count
    swings by a factor of three between Tuesday and Sunday and is worth an
    alert only when it leaves its own weekly envelope.
    """
    aoi = _aoi("IN-MUN-PORT", "Mundra Port — container terminal",
               (69.7050, 22.7390), 4600, 3400, AoiKind.PORT,
               "Container terminal, berths and landside yard.")
    seed = seed_of(aoi.id)
    terrain = Terrain(seed=seed, base_reflectance=0.30, roughness=0.16,
                      feature_scale_m=520.0, sar_base=0.26,
                      shoreline=("north", 300.0), shoreline_wobble_m=180.0)

    zones = [
        Zone("basin", -900, 950, 2600, 1000, (ObjectClass.VESSEL,)),
        Zone("anchorage", 1400, 1250, 1400, 700, (ObjectClass.VESSEL,)),
        Zone("yard", -400, -400, 1800, 900,
             (ObjectClass.CONTAINER_STACK, ObjectClass.TRUCK),
             surface_reflectance=0.34, surface_backscatter=0.40),
        Zone("gate", 1200, -900, 900, 500, (ObjectClass.TRUCK,),
             surface_reflectance=0.36, surface_backscatter=0.42),
    ]

    features = [
        Feature("MUN-QUAY-1", ObjectClass.BRIDGE, -900, 240, 2400, 90,
                reflectance=0.52, backscatter=0.80),
        Feature("MUN-WH-A", ObjectClass.BUILDING, -1100, -560, 220, 90,
                reflectance=0.64, backscatter=0.82),
        Feature("MUN-WH-B", ObjectClass.BUILDING, -760, -560, 220, 90,
                reflectance=0.63, backscatter=0.82),
        Feature("MUN-TANK-1", ObjectClass.STORAGE_TANK, 900, -300, 64, 64,
                reflectance=0.70, backscatter=0.88),
        Feature("MUN-TANK-2", ObjectClass.STORAGE_TANK, 1010, -300, 64, 64,
                reflectance=0.70, backscatter=0.88),
        Feature("MUN-CRANE-ROW", ObjectClass.TOWER, -300, 300, 900, 24,
                reflectance=0.74, backscatter=0.92),
        # The change the demo is built around: a third warehouse goes up, and a
        # quay extension follows it two months later.
        Feature("MUN-WH-C", ObjectClass.BUILDING, -420, -560, 240, 96,
                reflectance=0.66, backscatter=0.84,
                appears_on=date(2026, 4, 22)),
        Feature("MUN-QUAY-EXT", ObjectClass.BRIDGE, 620, 240, 620, 90,
                reflectance=0.54, backscatter=0.81,
                appears_on=date(2026, 6, 17)),
    ]

    activity = [
        ActivityProfile(ObjectClass.VESSEL, 5.0,
                        weekly=(1.25, 1.20, 1.10, 1.15, 1.30, 0.75, 0.45),
                        noise_sd=0.22, zone="basin"),
        ActivityProfile(ObjectClass.VESSEL, 3.0,
                        weekly=(1.0,) * 7, noise_sd=0.30, zone="anchorage"),
        ActivityProfile(ObjectClass.CONTAINER_STACK, 42.0,
                        weekly=(1.10, 1.10, 1.05, 1.05, 1.15, 0.90, 0.80),
                        noise_sd=0.10, zone="yard"),
        ActivityProfile(ObjectClass.TRUCK, 34.0,
                        weekly=(1.25, 1.20, 1.15, 1.15, 1.30, 0.60, 0.30),
                        noise_sd=0.18, zone="gate"),
    ]

    episodes = [
        Episode("MUN-EP-1", "Port congestion: vessel and truck concentration "
                            "well above the weekly envelope",
                date(2026, 5, 11), date(2026, 5, 26),
                {ObjectClass.VESSEL: 2.6, ObjectClass.TRUCK: 1.9,
                 ObjectClass.CONTAINER_STACK: 1.35}),
    ]

    truth = SiteTruth(aoi.id, aoi.kind, terrain, features, zones, activity,
                      episodes, seed=seed)
    return DemoSite(aoi, truth, COASTAL_MONSOON,
                    "Terminal expansion and a two-week congestion episode",
                    episode_label=episodes[0].label)


# ---------------------------------------------------------------------------
# Bhadla: critical energy infrastructure under construction
# ---------------------------------------------------------------------------

def bhadla() -> DemoSite:
    """One of the world's largest solar parks, still growing.

    The infrastructure demo, and the easiest one to verify: array blocks are
    large, dark, rectangular and appear on known dates, so detection recall
    here is a clean number rather than an argument.
    """
    aoi = _aoi("IN-BHD-SOLAR", "Bhadla Solar Park — sector 3",
               (71.9050, 27.5390), 4200, 3200, AoiKind.ENERGY,
               "Photovoltaic array blocks, substation and access roads.")
    seed = seed_of(aoi.id)
    terrain = Terrain(seed=seed, base_reflectance=0.42, roughness=0.10,
                      feature_scale_m=680.0, sar_base=0.24)

    zones = [
        Zone("laydown", 1300, -900, 900, 700,
             (ObjectClass.CONSTRUCTION_VEHICLE, ObjectClass.TRUCK),
             surface_reflectance=0.44, surface_backscatter=0.30),
        Zone("access", -1700, 0, 300, 2400, (ObjectClass.VEHICLE,),
             surface_reflectance=0.40, surface_backscatter=0.34),
    ]

    features = [
        Feature("BHD-SUB", ObjectClass.BUILDING, 1500, 900, 180, 120,
                reflectance=0.66, backscatter=0.86),
        Feature("BHD-TOWER-1", ObjectClass.TOWER, 1750, 620, 20, 20,
                reflectance=0.72, backscatter=0.90),
        Feature("BHD-ROAD-MAIN", ObjectClass.ROAD, 0, 1300, 3800, 14,
                reflectance=0.50, backscatter=0.36),
    ]
    # Existing array blocks. Photovoltaic panels are dark in optical and
    # smooth-but-tilted in SAR, which is why they read as a strong negative
    # change against desert.
    for i, (e, n) in enumerate([(-1200, 700), (-600, 700), (0, 700),
                                (-1200, 100), (-600, 100), (0, 100)]):
        features.append(Feature(f"BHD-ARR-{i}", ObjectClass.SOLAR_ARRAY, e, n,
                                520, 440, reflectance=0.12, backscatter=0.45))
    # Three new blocks commissioned through the year: the change to find.
    for i, (e, n, day) in enumerate([(600, 700, date(2026, 3, 19)),
                                     (600, 100, date(2026, 5, 28)),
                                     (0, -500, date(2026, 8, 6))]):
        features.append(Feature(f"BHD-ARR-NEW-{i}", ObjectClass.SOLAR_ARRAY,
                                e, n, 520, 440, reflectance=0.12,
                                backscatter=0.45, appears_on=day))

    activity = [
        ActivityProfile(ObjectClass.CONSTRUCTION_VEHICLE, 6.0,
                        weekly=(1.2, 1.2, 1.15, 1.15, 1.2, 0.7, 0.25),
                        noise_sd=0.25, zone="laydown"),
        ActivityProfile(ObjectClass.TRUCK, 8.0,
                        weekly=(1.2, 1.15, 1.1, 1.1, 1.2, 0.6, 0.2),
                        noise_sd=0.22, zone="laydown"),
        ActivityProfile(ObjectClass.VEHICLE, 11.0,
                        weekly=(1.15, 1.1, 1.1, 1.1, 1.15, 0.8, 0.5),
                        noise_sd=0.20, zone="access"),
    ]

    episodes = [
        Episode("BHD-EP-1", "Construction mobilisation ahead of block "
                            "commissioning: plant and haulage sharply up",
                date(2026, 5, 4), date(2026, 5, 27),
                {ObjectClass.CONSTRUCTION_VEHICLE: 3.4, ObjectClass.TRUCK: 2.8,
                 ObjectClass.VEHICLE: 1.6},
                feature_ids=("BHD-ARR-NEW-1",)),
    ]

    truth = SiteTruth(aoi.id, aoi.kind, terrain, features, zones, activity,
                      episodes, seed=seed)
    return DemoSite(aoi, truth, ARID,
                    "Three array blocks commissioned; one mobilisation episode",
                    episode_label=episodes[0].label)


# ---------------------------------------------------------------------------
# Rann of Kutch: remote-area monitoring
# ---------------------------------------------------------------------------

def kutch() -> DemoSite:
    """Salt flat with almost nothing on it, which is what makes it useful.

    PRD section 53's third demo. A near-empty background is the hardest test of
    a false-positive rate: any detector that reports changes here when nothing
    has happened will drown a real border sector in noise. It is also the case
    where SAR earns its keep, because the surface is wet and cloudy for a third
    of the year and the optical record simply stops.
    """
    aoi = _aoi("IN-KCH-SECTOR", "Rann of Kutch — sector 12",
               (70.5120, 23.9080), 5000, 3600, AoiKind.BORDER_SECTOR,
               "Remote salt flat, seasonal water, one metalled track.")
    seed = seed_of(aoi.id)
    terrain = Terrain(seed=seed, base_reflectance=0.47, roughness=0.13,
                      feature_scale_m=900.0, water_level=0.26, sar_base=0.20)

    zones = [
        Zone("track", 0, -200, 4200, 120, (ObjectClass.VEHICLE,),
             surface_reflectance=0.40, surface_backscatter=0.30),
        Zone("post", 1500, 800, 400, 300,
             (ObjectClass.VEHICLE, ObjectClass.TRUCK),
             surface_reflectance=0.44, surface_backscatter=0.34),
    ]

    features = [
        Feature("KCH-TRACK", ObjectClass.ROAD, 0, -200, 4200, 12,
                reflectance=0.52, backscatter=0.34),
        Feature("KCH-POST-1", ObjectClass.BUILDING, 1500, 800, 40, 26,
                reflectance=0.68, backscatter=0.84),
        Feature("KCH-MAST", ObjectClass.TOWER, 1560, 860, 8, 8,
                reflectance=0.74, backscatter=0.92),
        # A track extension, then two structures at its end.
        Feature("KCH-TRACK-EXT", ObjectClass.ROAD, -1400, 900, 1900, 10,
                heading_deg=28.0, reflectance=0.54, backscatter=0.36,
                appears_on=date(2026, 7, 9)),
        Feature("KCH-NEW-1", ObjectClass.BUILDING, -600, 1380, 44, 30,
                reflectance=0.66, backscatter=0.83,
                appears_on=date(2026, 7, 28)),
        Feature("KCH-NEW-2", ObjectClass.BUILDING, -520, 1380, 44, 30,
                reflectance=0.66, backscatter=0.83,
                appears_on=date(2026, 8, 11)),
    ]

    activity = [
        ActivityProfile(ObjectClass.VEHICLE, 3.0, weekly=(1.0,) * 7,
                        noise_sd=0.40, zone="track"),
        ActivityProfile(ObjectClass.VEHICLE, 2.0, weekly=(1.0,) * 7,
                        noise_sd=0.35, zone="post"),
    ]

    episodes = [
        Episode("KCH-EP-1", "Vehicle concentration at the new track terminus, "
                            "far outside anything in the site's record",
                date(2026, 7, 20), date(2026, 8, 14),
                {ObjectClass.VEHICLE: 6.0, ObjectClass.TRUCK: 4.0},
                feature_ids=("KCH-NEW-1", "KCH-NEW-2")),
    ]

    truth = SiteTruth(aoi.id, aoi.kind, terrain, features, zones, activity,
                      episodes, seed=seed)
    return DemoSite(aoi, truth, ARID,
                    "Track extension, two new structures, a vehicle concentration",
                    episode_label=episodes[0].label)


# ---------------------------------------------------------------------------
# Sardar Sarovar: reservoir extent
# ---------------------------------------------------------------------------

def sardar_sarovar() -> DemoSite:
    """A dam and its reservoir. The disaster and water-security case.

    Reservoir extent is the cleanest possible demonstration that change
    detection is measuring something physical: the drawdown is thousands of
    cells of unambiguous surface change, and the area it reports can be checked
    against the polygon that produced it.
    """
    aoi = _aoi("IN-SSD-DAM", "Sardar Sarovar — reservoir head",
               (73.7480, 21.8310), 4400, 3200, AoiKind.DAM,
               "Dam structure, spillway and upstream reservoir extent.")
    seed = seed_of(aoi.id)
    terrain = Terrain(seed=seed, base_reflectance=0.33, roughness=0.20,
                      feature_scale_m=700.0, sar_base=0.27,
                      shoreline=("west", 700.0), shoreline_wobble_m=260.0)

    zones = [
        Zone("crest", 1500, -200, 260, 700, (ObjectClass.VEHICLE,),
             surface_reflectance=0.58, surface_backscatter=0.60),
    ]

    features = [
        Feature("SSD-DAM", ObjectClass.BRIDGE, 1500, -200, 240, 700,
                reflectance=0.70, backscatter=0.90),
        Feature("SSD-POWERHOUSE", ObjectClass.BUILDING, 1180, -620, 160, 90,
                reflectance=0.64, backscatter=0.84),
        # Drawdown: a strip of the reservoir becomes exposed bank in April and
        # refills with the monsoon in July.
        Feature("SSD-DRAWDOWN", ObjectClass.SOLAR_ARRAY, -600, 500, 2600, 700,
                reflectance=0.46, backscatter=0.33,
                appears_on=date(2026, 4, 7), removed_on=date(2026, 7, 24)),
    ]

    activity = [
        ActivityProfile(ObjectClass.VEHICLE, 4.0,
                        weekly=(1.1, 1.1, 1.0, 1.0, 1.1, 0.9, 0.7),
                        noise_sd=0.25, zone="crest"),
    ]

    episodes = [
        Episode("SSD-EP-1", "Reservoir drawdown: exposed bank across the "
                            "upstream reach", date(2026, 4, 7), date(2026, 7, 23),
                {}, feature_ids=("SSD-DRAWDOWN",)),
    ]

    truth = SiteTruth(aoi.id, aoi.kind, terrain, features, zones, activity,
                      episodes, seed=seed)
    return DemoSite(aoi, truth, COASTAL_MONSOON,
                    "A seasonal drawdown and refill, measured as area",
                    episode_label=episodes[0].label)


BUILDERS = {
    "IN-MUN-PORT": mundra,
    "IN-BHD-SOLAR": bhadla,
    "IN-KCH-SECTOR": kutch,
    "IN-SSD-DAM": sardar_sarovar,
}

ALIASES = {
    "mundra": "IN-MUN-PORT", "port": "IN-MUN-PORT",
    "bhadla": "IN-BHD-SOLAR", "solar": "IN-BHD-SOLAR",
    "kutch": "IN-KCH-SECTOR", "sector": "IN-KCH-SECTOR",
    "sardar": "IN-SSD-DAM", "dam": "IN-SSD-DAM",
}


def load(name: str) -> DemoSite:
    key = ALIASES.get(name.lower().strip(), name.strip())
    if key not in BUILDERS:
        known = ", ".join(sorted(ALIASES))
        raise KeyError(f"unknown site {name!r}; try one of: {known}")
    return BUILDERS[key]()


def load_all() -> list[DemoSite]:
    return [BUILDERS[k]() for k in BUILDERS]
