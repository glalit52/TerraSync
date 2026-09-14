"""The entities an Earth-intelligence platform is actually made of.

Two things shape this schema and neither is storage convenience.

The first is that an analyst has to be able to defend a finding. So a
ChangeEvent does not merely carry a confidence number; it carries the scenes
it came from, the baseline it was measured against and the model version that
produced it. Anything that cannot be traced back to pixels is not a finding,
it is an opinion.

The second is PRD section 55: the system never says "threat detected". The
vocabulary here is deliberately analytical -- deviation, novelty, persistence
-- and the review workflow assumes a human decides what a change *means*. The
schema will not let you record an intent; it only lets you record what the
sensor saw and what a named analyst concluded.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum

from .geo import Ring, area_km2, bbox, centroid, validate_ring


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class Sensor(str, Enum):
    """Sensing modality. Determines what a change can and cannot mean."""

    OPTICAL = "optical"
    SAR = "sar"
    THERMAL = "thermal"


class Constellation(str, Enum):
    SENTINEL_2 = "sentinel-2"     # optical, 10 m, 5-day revisit
    SENTINEL_1 = "sentinel-1"     # C-band SAR, 10 m, 6-12 day revisit
    LANDSAT_9 = "landsat-9"       # optical, 30 m, 16-day revisit
    COMMERCIAL_VHR = "commercial-vhr"   # tasked, sub-metre, licensed API


class AoiKind(str, Enum):
    """What sort of place this is. Drives which detectors and rules apply."""

    PORT = "port"
    AIRFIELD = "airfield"
    BORDER_SECTOR = "border_sector"
    ENERGY = "energy"
    INDUSTRIAL = "industrial"
    DAM = "dam"
    TRANSPORT = "transport"
    GENERIC = "generic"


class ObjectClass(str, Enum):
    """Detector output classes. Extensible: the engine keys off the value."""

    BUILDING = "building"
    STORAGE_TANK = "storage_tank"
    TOWER = "tower"
    ROAD = "road"
    RUNWAY = "runway"
    BRIDGE = "bridge"
    VEHICLE = "vehicle"
    TRUCK = "truck"
    CONSTRUCTION_VEHICLE = "construction_vehicle"
    AIRCRAFT = "aircraft"
    HELICOPTER = "helicopter"
    VESSEL = "vessel"
    SMALL_BOAT = "small_boat"
    CONTAINER_STACK = "container_stack"
    SOLAR_ARRAY = "solar_array"


TRANSIENT_CLASSES = frozenset({
    ObjectClass.VEHICLE, ObjectClass.TRUCK, ObjectClass.CONSTRUCTION_VEHICLE,
    ObjectClass.AIRCRAFT, ObjectClass.HELICOPTER, ObjectClass.VESSEL,
    ObjectClass.SMALL_BOAT,
})
"""Classes whose count is an activity signal rather than an infrastructure fact.

The distinction matters: three new buildings is a change event, thirty extra
vehicles is a pattern-of-life deviation, and conflating them produces exactly
the alert fatigue PRD section 20 is trying to prevent.
"""


class ChangeType(str, Enum):
    NEW_STRUCTURE = "new_structure"
    #: A moveable object arrived or left. Separate from construction on
    #: purpose: a ship leaving a berth and a warehouse being demolished both
    #: show up as "the bright thing is gone", and treating them alike floods a
    #: port's change feed with forty events a week that mean nothing.
    OBJECT_APPEARED = "object_appeared"
    OBJECT_DEPARTED = "object_departed"
    STRUCTURE_REMOVED = "structure_removed"
    CONSTRUCTION_ACTIVITY = "construction_activity"
    LINEAR_FEATURE = "linear_feature"        # road or track extension
    SURFACE_CHANGE = "surface_change"
    POSSIBLE_DAMAGE = "possible_damage"
    INUNDATION = "inundation"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"low": 1, "medium": 2, "high": 3, "critical": 4}[self.value]


class SiteStatus(str, Enum):
    """What the analyst sees at the top of a site page.

    Deliberately three words, none of them a judgement about intent.
    """

    NORMAL = "normal"
    WATCH = "watch"
    ANOMALOUS = "anomalous"
    STALE = "stale"      # no usable imagery recently; absence of signal, not signal


class ReviewStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    ESCALATED = "escalated"


class Role(str, Enum):
    """Least privilege, in the order a government customer will ask for it."""

    VIEWER = "viewer"        # read alerts and sites
    ANALYST = "analyst"      # + review findings, create AOIs and rules
    SUPERVISOR = "supervisor"  # + escalate, publish reports
    ADMIN = "admin"          # + users, data sources, retention


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(*parts: object) -> str:
    blob = json.dumps(parts, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Tenancy and people
# ---------------------------------------------------------------------------

@dataclass
class Organization:
    """A tenant. Every row in the system belongs to exactly one.

    Tenant isolation is enforced at the store, not at the API, because an API
    is one forgotten filter away from a cross-tenant leak and a government
    customer will not accept "we test for that".
    """

    id: str
    name: str
    country: str
    classification: str = "UNCLASSIFIED"
    created_at: datetime = field(default_factory=_now)


@dataclass
class User:
    id: str
    org_id: str
    name: str
    email: str
    role: Role = Role.VIEWER
    active: bool = True
    created_at: datetime = field(default_factory=_now)

    def can(self, permission: str) -> bool:
        from .rbac import allows
        return self.active and allows(self.role, permission)


# ---------------------------------------------------------------------------
# Areas of interest
# ---------------------------------------------------------------------------

@dataclass
class Aoi:
    """A monitored place. The unit of everything: tasking, billing, baselines."""

    id: str
    org_id: str
    name: str
    boundary: Ring
    kind: AoiKind = AoiKind.GENERIC
    country: str = ""
    description: str = ""
    active: bool = True
    created_at: datetime = field(default_factory=_now)

    @property
    def area_km2(self) -> float:
        return area_km2(self.boundary)

    @property
    def centroid(self) -> tuple[float, float]:
        return centroid(self.boundary)

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return bbox(self.boundary)

    @property
    def fingerprint(self) -> str:
        """Stable identity of the geometry, so a redrawn AOI is a new baseline.

        Moving a boundary invalidates the pattern-of-life history built against
        the old one. Rather than silently carry it over, the fingerprint changes
        and the baseline starts again -- honest, and much easier to explain to a
        verifier than a baseline that quietly spans two different footprints.
        """
        return _fingerprint(self.id, [(round(x, 7), round(y, 7)) for x, y in self.boundary])

    def problems(self) -> list[str]:
        issues = validate_ring(self.boundary)
        if self.area_km2 > 50_000:
            issues.append(
                f"AOI covers {self.area_km2:,.0f} km2; split it -- a baseline "
                "over an area this large averages away the thing you are "
                "looking for")
        return issues


@dataclass
class Watchlist:
    """A named group of AOIs, monitored and reported on together."""

    id: str
    org_id: str
    name: str
    aoi_ids: list[str] = field(default_factory=list)
    priority: Severity = Severity.MEDIUM
    created_at: datetime = field(default_factory=_now)


# ---------------------------------------------------------------------------
# Imagery
# ---------------------------------------------------------------------------

@dataclass
class Scene:
    """One acquisition over one AOI. The atom of evidence.

    `usable` is the field that earns its keep. Most monitoring systems that
    disappoint do so because they quietly analysed a cloudy scene and reported
    no change. A scene that cannot support analysis is recorded, marked, and
    excluded -- and its exclusion is visible on the timeline.
    """

    id: str
    aoi_id: str
    constellation: Constellation
    sensor: Sensor
    acquired_at: datetime
    gsd_m: float
    cloud_pct: float = 0.0
    off_nadir_deg: float = 0.0
    sun_elevation_deg: float = 45.0
    orbit: str = ""
    checksum: str = ""

    #: Above this, an optical scene tells you about the weather, not the ground.
    CLOUD_LIMIT = 35.0

    @property
    def usable(self) -> bool:
        if self.sensor is Sensor.OPTICAL:
            return self.cloud_pct <= self.CLOUD_LIMIT and self.sun_elevation_deg >= 15.0
        return True   # SAR does not care about cloud or darkness; that is the point

    @property
    def unusable_reason(self) -> str:
        if self.usable:
            return ""
        if self.cloud_pct > self.CLOUD_LIMIT:
            return f"cloud cover {self.cloud_pct:.0f}% exceeds {self.CLOUD_LIMIT:.0f}% limit"
        return f"sun elevation {self.sun_elevation_deg:.0f} degrees is too low for optical analysis"

    @property
    def acquired_on(self) -> date:
        return self.acquired_at.date()


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

@dataclass
class Detection:
    """One object the detector believes it found in one scene."""

    id: str
    aoi_id: str
    scene_id: str
    object_class: ObjectClass
    confidence: float               # 0..1, calibrated against the contrast margin
    lon: float
    lat: float
    geometry: Ring                  # footprint, WGS84
    extent_m: float = 0.0           # longest ground dimension
    model_version: str = ""
    observed_at: datetime = field(default_factory=_now)

    @property
    def transient(self) -> bool:
        return self.object_class in TRANSIENT_CLASSES


@dataclass
class ChangeEvent:
    """A difference between two scenes, with everything needed to defend it."""

    id: str
    aoi_id: str
    change_type: ChangeType
    detected_at: datetime
    before_scene_id: str
    after_scene_id: str
    geometry: Ring
    area_m2: float
    confidence: float
    magnitude: float                # mean absolute radiometric difference, 0..1
    severity: Severity = Severity.LOW
    explanation: str = ""
    model_version: str = ""
    evidence_id: str = ""
    review_status: ReviewStatus = ReviewStatus.PENDING
    reviewed_by: str = ""
    reviewed_at: datetime | None = None
    review_note: str = ""

    @property
    def centroid(self) -> tuple[float, float]:
        return centroid(self.geometry)


@dataclass
class BaselineStat:
    """What normal looks like for one metric at one AOI.

    Median and MAD rather than mean and standard deviation, because a single
    cloudy scene or one genuinely unusual day would otherwise poison the
    definition of normal for weeks.
    """

    aoi_id: str
    metric: str
    median: float
    mad: float
    n: int
    window_start: date
    window_end: date

    def z(self, value: float) -> float:
        """Robust deviation in MAD-scaled units, comparable to a z-score."""
        scale = 1.4826 * self.mad
        if scale < 1e-9:
            #: A metric whose MAD has collapsed cannot express small
            #: deviations, and a short or degenerate sample collapses it
            #: easily. Fall back to counting noise rather than to a fixed
            #: floor: for a count, the spread scales with the square root of
            #: the level, so a median of 2 tolerates about 1.4 and a median of
            #: 40 about 6.3. A flat proportional floor instead makes every
            #: small count a three-sigma event on any day the detector is one
            #: object less consistent than usual.
            floor = max(1.0, math.sqrt(max(abs(self.median), 1.0)))
            return (value - self.median) / floor
        return (value - self.median) / scale


@dataclass
class AnomalyFinding:
    """A deviation from the site's own history. Not an assessment of intent."""

    id: str
    aoi_id: str
    observed_at: datetime
    score: float                    # 0..100
    confidence: float               # 0..1
    reasons: list[str] = field(default_factory=list)
    contributions: dict[str, float] = field(default_factory=dict)
    baseline_n: int = 0
    evidence_id: str = ""
    review_status: ReviewStatus = ReviewStatus.PENDING

    @property
    def severity(self) -> Severity:
        if self.score >= 85:
            return Severity.CRITICAL
        if self.score >= 65:
            return Severity.HIGH
        if self.score >= 40:
            return Severity.MEDIUM
        return Severity.LOW


@dataclass
class Alert:
    """A finding a rule decided a human should see, with its place in the queue."""

    id: str
    org_id: str
    aoi_id: str
    rule_id: str
    title: str
    severity: Severity
    priority: int                   # 1 is most urgent; see alerts.prioritise
    created_at: datetime
    summary: str = ""
    change_event_ids: list[str] = field(default_factory=list)
    anomaly_ids: list[str] = field(default_factory=list)
    evidence_id: str = ""
    review_status: ReviewStatus = ReviewStatus.PENDING
    reviewed_by: str = ""
    reviewed_at: datetime | None = None
    review_note: str = ""
    delivered_to: list[str] = field(default_factory=list)


@dataclass
class Report:
    id: str
    org_id: str
    kind: str                       # daily | weekly | site
    subject: str                    # AOI id, watchlist id, or the org
    period_start: date
    period_end: date
    generated_at: datetime
    body_markdown: str = ""
    alert_ids: list[str] = field(default_factory=list)
