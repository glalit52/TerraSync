"""Imagery providers: choose one by name, configure it with environment variables.

The requirement: run on free imagery today, and add a paid vendor later by
supplying keys rather than by editing code.

    TERRASHIELD_PROVIDER=earth-search          # free, no account (the default)
    TERRASHIELD_PROVIDER=cdse                  # free, needs a free registration
    TERRASHIELD_CDSE_CLIENT_ID=...
    TERRASHIELD_CDSE_CLIENT_SECRET=...

    TERRASHIELD_PROVIDER=planet                # when the subscription exists
    TERRASHIELD_PLANET_API_KEY=...

    TERRASHIELD_PROVIDER=synthetic             # the modelled estate, offline

Nothing above `catalog.Provider` changes when that variable changes. That is
the property the whole design is for: the pipeline cannot tell a synthetic
provider from Copernicus from Maxar, so switching vendors is a deployment
decision rather than a rewrite.

`status()` is what the CLI prints. It reports every known vendor, whether its
credentials are present, and the exact variable names to set -- and never a
secret value.
"""

from __future__ import annotations

import os

from .credentials import Credentials, CredentialError
from .presets import (
    ALIASES, DEFAULT_PRESET, PRESETS, CollectionSpec, ProviderPreset, preset,
)
from .stac import PixelsUnavailable, ProviderUnavailable, StacProvider
from .transport import Transport, TransportError, UrllibTransport

__all__ = [
    "ALIASES", "PRESETS", "DEFAULT_PRESET", "CollectionSpec", "Credentials",
    "CredentialError", "PixelsUnavailable", "ProviderPreset",
    "ProviderUnavailable", "StacProvider", "Transport", "TransportError",
    "UrllibTransport", "available", "configured_name", "preset", "resolve",
    "status",
]

#: The name that means "use the modelled estate". Not a STAC catalogue, so it
#: is handled separately rather than being given a fake preset.
SYNTHETIC = "synthetic"

ENV_PROVIDER = "TERRASHIELD_PROVIDER"


def configured_name(env: dict[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    return (source.get(ENV_PROVIDER, "") or DEFAULT_PRESET).strip().lower()


def resolve(name: str = "", *, env: dict[str, str] | None = None,
            transport: Transport | None = None):
    """The provider named, or the one the environment selects.

    Returns anything satisfying `catalog.Provider`. `synthetic` returns the
    modelled estate with the demo sites registered, so the offline path stays
    one word rather than a different code path.
    """
    chosen = (name or configured_name(env)).strip().lower()
    chosen = ALIASES.get(chosen, chosen)

    if chosen in (SYNTHETIC, "demo", "modelled", "modeled"):
        return _synthetic()

    spec = preset(chosen)
    credentials = Credentials.from_env(spec.name, spec.auth, env=env,
                                       transport=transport)
    return StacProvider(preset=spec, credentials=credentials,
                        transport=transport or UrllibTransport())


def _synthetic():
    from ..catalog import SyntheticProvider       # noqa: PLC0415
    from .. import sites                          # noqa: PLC0415
    provider = SyntheticProvider()
    for demo in sites.load_all():
        provider.register(demo.truth, demo.climate)
    return provider


def available(env: dict[str, str] | None = None) -> list[str]:
    """Vendors that could be used right now, without anything being added."""
    ready = [SYNTHETIC]
    for name, spec in PRESETS.items():
        credentials = Credentials.from_env(name, spec.auth, env=env)
        if credentials.complete:
            ready.append(name)
    return ready


def status(env: dict[str, str] | None = None) -> dict:
    """Every vendor, whether it is usable, and what is missing if not.

    Deliberately never contacts a vendor: this answers "is it configured",
    which is the question somebody setting the product up is asking, and it
    must work with no network at all. `StacProvider.health()` answers "can it
    be reached", which is a different and slower question.
    """
    selected = configured_name(env)
    selected = ALIASES.get(selected, selected)
    entries = []
    for name, spec in sorted(PRESETS.items()):
        credentials = Credentials.from_env(name, spec.auth, env=env)
        entries.append({
            "name": name,
            "title": spec.title,
            "selected": name == selected,
            "free": spec.free,
            "needs_account": spec.needs_account,
            "ready": credentials.complete,
            "missing": [f.env_var(name) for f in credentials.missing],
            "constellations": [c.value for c in spec.constellations],
            "notes": spec.notes,
        })
    return {
        "selected": selected,
        "select_with": f"{ENV_PROVIDER}=<name>",
        "synthetic_available": True,
        "providers": entries,
    }
