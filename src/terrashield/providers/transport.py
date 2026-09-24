"""The HTTP seam.

Every outbound call a provider makes goes through a `Transport`. There is one
real implementation built on `urllib`, and tests substitute their own. That
matters more than it sounds: an imagery client that can only be exercised
against a live vendor is an imagery client that is never exercised, because
running the suite would need credentials, network and somebody's quota.

Nothing here is vendor-specific. Retries, timeouts and error mapping are the
same problem whether the other end is Copernicus or Planet.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol


class TransportError(Exception):
    """A request failed in a way the caller may be able to act on."""

    def __init__(self, message: str, status: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.body = body


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        try:
            return json.loads(self.body or b"{}")
        except json.JSONDecodeError as e:
            raise TransportError(
                f"response was not JSON: {e}", self.status,
                self.body[:400].decode("utf-8", "replace")) from e


class Transport(Protocol):
    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                body: bytes | None = None, timeout: float = 30.0) -> Response: ...


#: Statuses worth trying again. 429 and 5xx are the vendor asking for patience;
#: everything else is the request being wrong, and repeating it just spends
#: quota to get the same answer.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 4
BACKOFF_BASE = 1.5


@dataclass
class UrllibTransport:
    """The real one. Standard library only, so the core stays dependency-free."""

    user_agent: str = "TerraShield/0.1 (+https://github.com/glalit52/terrasync)"
    max_attempts: int = MAX_ATTEMPTS
    sleep: Any = time.sleep

    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                body: bytes | None = None, timeout: float = 30.0) -> Response:
        sent = {"User-Agent": self.user_agent, **(headers or {})}
        last: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            req = urllib.request.Request(url, method=method, data=body,
                                         headers=sent)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as raw:
                    return Response(raw.status, raw.read(),
                                    {k.lower(): v for k, v in raw.headers.items()})
            except urllib.error.HTTPError as e:
                detail = e.read()[:2000].decode("utf-8", "replace")
                if e.code in RETRY_STATUSES and attempt < self.max_attempts:
                    #: Honour Retry-After when the vendor sends one. Guessing
                    #: an interval when you have been told the right one is how
                    #: a client gets itself rate-limited harder.
                    wait = _retry_after(e.headers) or BACKOFF_BASE ** attempt
                    self.sleep(wait)
                    last = e
                    continue
                raise TransportError(
                    f"{method} {_safe(url)} failed: HTTP {e.code}", e.code,
                    detail) from e
            except urllib.error.URLError as e:
                #: No status: DNS, TLS, a refused connection, or an egress
                #: policy that denied the host. All worth one retry, none worth
                #: four.
                if attempt < self.max_attempts:
                    self.sleep(BACKOFF_BASE ** attempt)
                    last = e
                    continue
                raise TransportError(
                    f"{method} {_safe(url)} could not be reached: {e.reason}. "
                    "If this is an egress policy rather than the vendor, the "
                    "host has to be allowed before any imagery will arrive."
                ) from e

        raise TransportError(f"{method} {_safe(url)} failed after "
                             f"{self.max_attempts} attempts: {last}")


def _retry_after(headers) -> float | None:
    raw = (headers or {}).get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, min(60.0, float(raw)))
    except (TypeError, ValueError):
        return None


def _safe(url: str) -> str:
    """A URL with its query string dropped.

    Some vendors accept an API key as a query parameter, and an exception
    message is one of the easiest ways for a credential to end up in a log
    file, a bug report or a screenshot.
    """
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
