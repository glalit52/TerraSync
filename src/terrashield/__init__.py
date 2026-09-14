"""TerraShield — AI-powered Earth observation and geospatial intelligence.

An analyst-assistance and monitoring system: it converts satellite imagery into
detections, changes, deviations from a site's own history, and ranked alerts,
each carrying the evidence it rests on.

Three principles run through every module, and they are product decisions
before they are engineering ones.

**Say what the sensor could not see.** A count of zero from a 10 m scene is not
the same statement as "there were none", and a quiet week and a cloudy week are
not the same week. Coverage, resolution limits and cloud gaps are first-class
outputs, not footnotes.

**Carry the evidence with the finding.** Every change and every anomaly returns
an evidence bundle: the scenes compared, the masks applied, the baseline used,
the model version, and what could not be established. A finding that cannot be
traced back to pixels is an opinion.

**Never assert intent.** The system reports deviation from a site's own record.
There is no field anywhere in this schema in which a motive could be stored,
and the copilot refuses questions that ask for one. Imagery does not contain
intentions, and a platform that pretends otherwise spends its credibility the
first time it is confidently wrong.
"""

__version__ = "0.1.0"

from .domain import (
    Alert,
    AnomalyFinding,
    Aoi,
    AoiKind,
    ChangeEvent,
    ChangeType,
    Constellation,
    Detection,
    ObjectClass,
    Organization,
    Role,
    Scene,
    Sensor,
    Severity,
    SiteStatus,
    User,
    Watchlist,
)
from .evidence import EvidenceBundle
from .raster import Raster

__all__ = [
    "Aoi", "AoiKind", "Scene", "Sensor", "Constellation", "Detection",
    "ObjectClass", "ChangeEvent", "ChangeType", "AnomalyFinding", "Alert",
    "Severity", "SiteStatus", "Organization", "User", "Role", "Watchlist",
    "EvidenceBundle", "Raster", "__version__",
]
