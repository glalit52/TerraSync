"""The vendors, as data.

Almost every imagery vendor worth integrating exposes a STAC catalogue, and
their item properties are standardised by the same extensions -- `eo:cloud_cover`,
`view:off_nadir`, `view:sun_elevation`, `sat:relative_orbit`. That is the whole
reason this file is a table rather than five client libraries: what actually
differs between Copernicus and Planet is an endpoint, a collection name, an
auth style and an asset key.

So adding a vendor is adding a `ProviderPreset`. Adding a *subscription* to a
vendor already listed is setting two environment variables and changing one
setting, with no code involved at all -- which is the requirement this serves.

Two of these are free and need no account:

  earth-search        Sentinel-1, Sentinel-2 and Landsat on AWS open data
  planetary-computer  the same missions, hosted by Microsoft

One is free but needs a (free) registration:

  cdse                Copernicus Data Space Ecosystem, the official ESA source

The rest are commercial and inert until their keys are present.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain import Constellation, Sensor
from .credentials import ANONYMOUS, AuthSpec, api_key_header, oauth2


@dataclass(frozen=True)
class CollectionSpec:
    """One mission, as one vendor names it."""

    collection: str
    constellation: Constellation
    sensor: Sensor
    gsd_m: float
    #: Asset keys in preference order. Vendors disagree about naming even for
    #: the same product -- "red" on Earth Search is "B04" on CDSE -- and a list
    #: means the resolver can try what exists rather than assume.
    visible_assets: tuple[str, ...] = ()
    #: The quality band that ships with the product. Never re-derived from
    #: brightness: doing that is what reported a new solar block as eleven
    #: hectares of inundation on a desert energy site.
    quality_asset: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderPreset:
    """Everything that differs between one vendor and another."""

    name: str
    title: str
    stac_url: str
    auth: AuthSpec
    collections: tuple[CollectionSpec, ...]
    free: bool = False
    needs_account: bool = False
    notes: str = ""
    #: Some catalogues want a vendor-specific parameter on every search.
    extra_search: dict = field(default_factory=dict)

    def for_constellation(self, c: Constellation) -> CollectionSpec | None:
        for spec in self.collections:
            if spec.constellation is c:
                return spec
        return None

    @property
    def constellations(self) -> tuple[Constellation, ...]:
        return tuple(s.constellation for s in self.collections)


# ---------------------------------------------------------------------------
# Free, no account
# ---------------------------------------------------------------------------

EARTH_SEARCH = ProviderPreset(
    name="earth-search",
    title="AWS Earth Search (Element 84)",
    stac_url="https://earth-search.aws.element84.com/v1",
    auth=ANONYMOUS,
    free=True,
    needs_account=False,
    notes=("Sentinel and Landsat on AWS open data. No account, no key, no "
           "quota. The right default for a pilot, and the cheapest way to "
           "find out whether the thresholds transfer to real imagery."),
    collections=(
        CollectionSpec("sentinel-2-l2a", Constellation.SENTINEL_2, Sensor.OPTICAL,
                       10.0, visible_assets=("red", "B04"),
                       quality_asset=("scl", "SCL")),
        CollectionSpec("sentinel-1-grd", Constellation.SENTINEL_1, Sensor.SAR,
                       10.0, visible_assets=("vv", "VV")),
        CollectionSpec("landsat-c2-l2", Constellation.LANDSAT_9, Sensor.OPTICAL,
                       30.0, visible_assets=("red", "SR_B4"),
                       quality_asset=("qa_pixel", "QA_PIXEL")),
    ),
)

PLANETARY_COMPUTER = ProviderPreset(
    name="planetary-computer",
    title="Microsoft Planetary Computer",
    stac_url="https://planetarycomputer.microsoft.com/api/stac/v1",
    auth=ANONYMOUS,
    free=True,
    needs_account=False,
    notes=("Search is open. Asset URLs are time-limited and must be signed "
           "through the /sas endpoint before the pixels can be read, which is "
           "why this is listed as search-ready rather than fetch-ready."),
    collections=(
        CollectionSpec("sentinel-2-l2a", Constellation.SENTINEL_2, Sensor.OPTICAL,
                       10.0, visible_assets=("B04", "red"),
                       quality_asset=("SCL", "scl")),
        CollectionSpec("sentinel-1-grd", Constellation.SENTINEL_1, Sensor.SAR,
                       10.0, visible_assets=("vv", "VV")),
        CollectionSpec("landsat-c2-l2", Constellation.LANDSAT_9, Sensor.OPTICAL,
                       30.0, visible_assets=("red", "SR_B4"),
                       quality_asset=("qa_pixel", "QA_PIXEL")),
    ),
)

# ---------------------------------------------------------------------------
# Free, account required
# ---------------------------------------------------------------------------

CDSE = ProviderPreset(
    name="cdse",
    title="Copernicus Data Space Ecosystem (ESA)",
    stac_url="https://catalogue.dataspace.copernicus.eu/stac",
    auth=oauth2(
        "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/"
        "openid-connect/token"),
    free=True,
    needs_account=True,
    notes=("The official ESA source, free after registering at "
           "dataspace.copernicus.eu. Register, create an OAuth client, then "
           "set the two variables below. Preferred over the mirrors when "
           "completeness or latency matters, because it is the source."),
    collections=(
        CollectionSpec("SENTINEL-2", Constellation.SENTINEL_2, Sensor.OPTICAL,
                       10.0, visible_assets=("B04",), quality_asset=("SCL",)),
        CollectionSpec("SENTINEL-1", Constellation.SENTINEL_1, Sensor.SAR,
                       10.0, visible_assets=("VV", "vv")),
    ),
)

SENTINEL_HUB = ProviderPreset(
    name="sentinel-hub",
    title="Sentinel Hub (Planet)",
    stac_url="https://services.sentinel-hub.com/api/v1/catalog/1.0.0",
    auth=oauth2("https://services.sentinel-hub.com/auth/realms/main/protocol/"
                "openid-connect/token"),
    free=False,
    needs_account=True,
    notes=("Commercial processing API over the same free missions. Worth "
           "paying for when you want server-side mosaicking and resampling "
           "rather than raw scenes."),
    collections=(
        CollectionSpec("sentinel-2-l2a", Constellation.SENTINEL_2, Sensor.OPTICAL,
                       10.0, visible_assets=("B04",), quality_asset=("SCL",)),
        CollectionSpec("sentinel-1-grd", Constellation.SENTINEL_1, Sensor.SAR,
                       10.0, visible_assets=("VV",)),
    ),
)

# ---------------------------------------------------------------------------
# Commercial. Inert until their keys are present.
# ---------------------------------------------------------------------------

PLANET = ProviderPreset(
    name="planet",
    title="Planet Labs",
    stac_url="https://api.planet.com/data/v1",
    auth=api_key_header("Authorization", "api-key {api_key}"),
    free=False,
    needs_account=True,
    notes=("Daily 3 m PlanetScope and tasked 50 cm SkySat. The revisit is the "
           "product: daily coverage is what makes a two-week construction "
           "sequence visible rather than a before-and-after pair."),
    collections=(
        CollectionSpec("PSScene", Constellation.COMMERCIAL_VHR, Sensor.OPTICAL,
                       3.0, visible_assets=("ortho_analytic_4b", "basic_analytic_4b"),
                       quality_asset=("ortho_udm2", "udm2")),
        CollectionSpec("SkySatCollect", Constellation.COMMERCIAL_VHR,
                       Sensor.OPTICAL, 0.5, visible_assets=("ortho_analytic",)),
    ),
)

MAXAR = ProviderPreset(
    name="maxar",
    title="Maxar",
    stac_url="https://api.maxar.com/discovery/v1",
    auth=api_key_header("Maxar-API-Key", "{api_key}"),
    free=False,
    needs_account=True,
    notes=("30 cm WorldView. The resolution at which vehicle-level pattern of "
           "life is actually possible -- at 10 m a car is a fifth of a pixel, "
           "and no model recovers it."),
    collections=(
        CollectionSpec("wv04", Constellation.COMMERCIAL_VHR, Sensor.OPTICAL,
                       0.31, visible_assets=("visual", "pan")),
        CollectionSpec("wv03-vnir", Constellation.COMMERCIAL_VHR, Sensor.OPTICAL,
                       0.31, visible_assets=("visual", "pan")),
    ),
)

UMBRA = ProviderPreset(
    name="umbra",
    title="Umbra Space (SAR)",
    stac_url="https://api.canopy.umbra.space/stac",
    auth=api_key_header("Authorization", "Bearer {api_key}"),
    free=False,
    needs_account=True,
    notes=("Tasked sub-metre SAR. The answer to a monsoon coast, where optical "
           "delivers roughly one usable look a month for four months a year."),
    collections=(
        CollectionSpec("umbra-sar", Constellation.COMMERCIAL_VHR, Sensor.SAR,
                       0.25, visible_assets=("GEC", "SIDD")),
    ),
)


PRESETS: dict[str, ProviderPreset] = {
    p.name: p for p in (
        EARTH_SEARCH, PLANETARY_COMPUTER, CDSE, SENTINEL_HUB,
        PLANET, MAXAR, UMBRA,
    )
}

#: What `terrashield` uses when nothing is configured. Free, anonymous, and
#: carrying the missions the engines were tuned against.
DEFAULT_PRESET = EARTH_SEARCH.name

#: Aliases, because people ask for the mission rather than the vendor.
ALIASES = {
    "sentinel": EARTH_SEARCH.name,
    "sentinel-2": EARTH_SEARCH.name,
    "sentinel-1": EARTH_SEARCH.name,
    "copernicus": CDSE.name,
    "esa": CDSE.name,
    "aws": EARTH_SEARCH.name,
    "element84": EARTH_SEARCH.name,
    "mpc": PLANETARY_COMPUTER.name,
    "microsoft": PLANETARY_COMPUTER.name,
    "shub": SENTINEL_HUB.name,
}


def preset(name: str) -> ProviderPreset:
    key = (name or "").strip().lower()
    key = ALIASES.get(key, key)
    if key not in PRESETS:
        known = ", ".join(sorted(PRESETS))
        raise KeyError(f"unknown imagery provider {name!r}; known: {known}")
    return PRESETS[key]
