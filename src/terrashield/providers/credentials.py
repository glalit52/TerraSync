"""Credentials, and how a vendor gets switched on without touching code.

The requirement this serves: run on free imagery today, and add a paid vendor
later by supplying keys rather than by editing Python. So a vendor is described
by data -- which secrets it needs, and how they attach to a request -- and
everything else is shared.

Secrets come from the environment, under a fixed naming convention:

    TERRASHIELD_<VENDOR>_<FIELD>

so Copernicus client credentials are TERRASHIELD_CDSE_CLIENT_ID and
TERRASHIELD_CDSE_CLIENT_SECRET, and a Planet key is TERRASHIELD_PLANET_API_KEY.
There is no config file holding secrets on purpose: a file gets committed, and
the failure is silent and permanent.

Four attachment styles cover every vendor here:

  none    the catalogue is open (AWS Earth Search, Planetary Computer search)
  header  a static key in a header (Planet, Maxar)
  oauth2  client-credentials exchanged for a bearer token (Copernicus, Sentinel Hub)
  basic   username and password (a few legacy ESA endpoints)

`describe()` is what the CLI prints. It never prints a secret -- only whether
one is present, and the exact variable name to set if it is not, because "auth
failed" without naming the variable is the least useful error a product can
give someone setting it up.
"""

from __future__ import annotations

import base64
import os
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable

from .transport import Transport, TransportError, UrllibTransport


class CredentialError(Exception):
    """A vendor is configured but its secrets are missing or were rejected."""


@dataclass(frozen=True)
class CredentialField:
    """One secret a vendor needs."""

    key: str                     # logical name, e.g. "client_id"
    env_suffix: str              # the part after TERRASHIELD_<VENDOR>_
    description: str
    secret: bool = True          # never echoed back, even when present

    def env_var(self, vendor: str) -> str:
        return f"TERRASHIELD_{vendor.upper().replace('-', '_')}_{self.env_suffix}"


@dataclass(frozen=True)
class AuthSpec:
    """How a vendor's credentials attach to a request."""

    kind: str = "none"                      # none | header | oauth2 | basic
    fields: tuple[CredentialField, ...] = ()
    header_name: str = "Authorization"
    header_format: str = "{api_key}"        # for kind="header"
    token_url: str = ""                     # for kind="oauth2"
    scope: str = ""

    @property
    def needs_credentials(self) -> bool:
        return self.kind != "none"


#: Access tokens are refreshed this many seconds before they actually expire,
#: so a request cannot be issued with a token that dies in flight.
TOKEN_REFRESH_MARGIN = 60.0


@dataclass
class Credentials:
    """Resolved secrets for one vendor, and the headers they produce."""

    vendor: str
    auth: AuthSpec
    values: dict[str, str] = field(default_factory=dict)
    transport: Transport = field(default_factory=UrllibTransport)
    clock: Callable[[], float] = time.time

    _token: str = field(default="", repr=False)
    _token_expires: float = field(default=0.0, repr=False)

    # -- construction ------------------------------------------------------

    @classmethod
    def from_env(cls, vendor: str, auth: AuthSpec,
                 env: dict[str, str] | None = None,
                 transport: Transport | None = None) -> "Credentials":
        source = os.environ if env is None else env
        values = {}
        for spec in auth.fields:
            raw = source.get(spec.env_var(vendor), "").strip()
            if raw:
                values[spec.key] = raw
        return cls(vendor=vendor, auth=auth, values=values,
                   transport=transport or UrllibTransport())

    # -- status ------------------------------------------------------------

    @property
    def missing(self) -> list[CredentialField]:
        return [f for f in self.auth.fields if not self.values.get(f.key)]

    @property
    def complete(self) -> bool:
        return not self.missing

    def describe(self) -> dict:
        """Configuration status, with no secret in it."""
        return {
            "vendor": self.vendor,
            "auth": self.auth.kind,
            "configured": self.complete,
            "provides": [
                {"field": f.key, "env": f.env_var(self.vendor),
                 "present": bool(self.values.get(f.key)),
                 "description": f.description}
                for f in self.auth.fields
            ],
            "missing": [f.env_var(self.vendor) for f in self.missing],
        }

    def require(self) -> None:
        if self.complete:
            return
        names = ", ".join(f.env_var(self.vendor) for f in self.missing)
        raise CredentialError(
            f"{self.vendor} needs credentials that are not set: {names}. "
            "Set them in the environment; they are deliberately not read from "
            "a file, because a file gets committed.")

    # -- use ---------------------------------------------------------------

    def headers(self) -> dict[str, str]:
        """Headers to attach to a catalogue or asset request."""
        kind = self.auth.kind
        if kind == "none":
            return {}
        self.require()
        if kind == "header":
            return {self.auth.header_name:
                    self.auth.header_format.format(**self.values)}
        if kind == "basic":
            raw = f"{self.values['username']}:{self.values['password']}"
            token = base64.b64encode(raw.encode()).decode()
            return {"Authorization": f"Basic {token}"}
        if kind == "oauth2":
            return {"Authorization": f"Bearer {self.access_token()}"}
        raise CredentialError(f"unknown auth kind {kind!r} for {self.vendor}")

    def access_token(self) -> str:
        """A live OAuth2 bearer token, fetched and cached until it nears expiry."""
        if self.auth.kind != "oauth2":
            raise CredentialError(f"{self.vendor} does not use OAuth2")
        self.require()
        now = self.clock()
        if self._token and now < self._token_expires - TOKEN_REFRESH_MARGIN:
            return self._token

        form = {"grant_type": "client_credentials",
                "client_id": self.values["client_id"],
                "client_secret": self.values["client_secret"]}
        if self.auth.scope:
            form["scope"] = self.auth.scope
        try:
            response = self.transport.request(
                "POST", self.auth.token_url,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                body=urllib.parse.urlencode(form).encode())
        except TransportError as e:
            #: The body of a token failure often names the actual problem
            #: ("invalid_client", "account not activated"), and hiding it makes
            #: setup guesswork. It cannot contain the secret -- that went the
            #: other way.
            raise CredentialError(
                f"{self.vendor} rejected the credentials: {e.message}"
                + (f" -- {e.body[:300]}" if e.body else "")) from e

        payload = response.json()
        token = payload.get("access_token", "")
        if not token:
            raise CredentialError(
                f"{self.vendor} returned no access_token in its token response")
        self._token = token
        self._token_expires = now + float(payload.get("expires_in", 600) or 600)
        return token

    def invalidate(self) -> None:
        """Drop the cached token, so the next call fetches a fresh one."""
        self._token = ""
        self._token_expires = 0.0


#: The open case, shared by every anonymous catalogue.
ANONYMOUS = AuthSpec(kind="none")


def oauth2(token_url: str, scope: str = "") -> AuthSpec:
    return AuthSpec(
        kind="oauth2", token_url=token_url, scope=scope,
        fields=(
            CredentialField("client_id", "CLIENT_ID",
                            "OAuth2 client id issued by the vendor"),
            CredentialField("client_secret", "CLIENT_SECRET",
                            "OAuth2 client secret issued by the vendor"),
        ))


def api_key_header(header_name: str = "Authorization",
                   header_format: str = "api-key {api_key}") -> AuthSpec:
    return AuthSpec(
        kind="header", header_name=header_name, header_format=header_format,
        fields=(CredentialField("api_key", "API_KEY",
                                "API key from the vendor's account page"),))


def basic_auth() -> AuthSpec:
    return AuthSpec(
        kind="basic",
        fields=(
            CredentialField("username", "USERNAME", "account username",
                            secret=False),
            CredentialField("password", "PASSWORD", "account password"),
        ))
