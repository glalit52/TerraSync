"""Getting an alert to a person, and proving it arrived.

`alerts.py` has always been able to raise one. Nothing sent it anywhere, which
made the alert rules a description of intent rather than a mechanism -- an
alerting system that does not alert is a queue.

Three decisions worth stating, because each is a place where notification
systems leak the thing they were protecting:

**The payload carries references, never content.** A webhook endpoint is
usually the least protected surface a customer operates: a chat integration, a
ticketing system, someone's automation. Sending the finding's text, imagery or
coordinates there moves intelligence out of the platform that logs who read it
and into one that does not. So the payload is an identifier, a severity, an
area id and a URL -- enough to know something needs attention, not enough to be
worth intercepting. `redact` enforces this rather than leaving it to whoever
writes the next channel.

**Every delivery is signed.** HMAC-SHA256 over the exact body, with a
timestamp, so a receiver can verify the message came from this platform and is
not a replay. The signature covers the timestamp precisely so that capturing a
valid delivery and resending it later fails.

**A failed delivery is recorded, not retried forever.** Attempts back off and
then stop, and the attempt is kept with its reason. An alert that silently
failed to send is worse than one that was never configured, because the
operator believes someone was told.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

from .providers.transport import Transport, TransportError, UrllibTransport


class Channel(str, Enum):
    WEBHOOK = "webhook"
    EMAIL = "email"


class Outcome(str, Enum):
    DELIVERED = "delivered"
    FAILED = "failed"
    SUPPRESSED = "suppressed"     # deliberately not sent; the reason is kept


#: How many times to try before giving up and recording the failure. Four
#: attempts over roughly half a minute covers a restart or a brief outage at
#: the far end; beyond that the endpoint is down rather than busy, and a queue
#: of retries becomes its own incident.
MAX_ATTEMPTS = 4
BACKOFF_BASE = 2.0

#: Deliveries older than this are refused by a correct receiver. Five minutes
#: is long enough to survive clock skew between two hosts and short enough that
#: a captured delivery is not useful later.
REPLAY_WINDOW_S = 300

#: Fields that may cross the boundary into a webhook payload. Everything else
#: stays in the platform, behind the audit log.
ALLOWED_FIELDS = frozenset({
    "alert_id", "aoi_id", "priority", "severity", "raised_at", "rule",
    "kind", "url", "platform", "version",
})


class DeliveryError(Exception):
    """A destination is misconfigured in a way the operator must fix."""


@dataclass(frozen=True)
class Destination:
    """Where alerts go, and what they are allowed to carry."""

    id: str
    channel: Channel
    target: str                       # a URL, or an address
    secret: str = ""                  # for webhook signing
    min_priority: int = 4             # 1 is most urgent; send at or above
    enabled: bool = True

    def accepts(self, priority: int) -> bool:
        return self.enabled and priority <= self.min_priority


@dataclass
class Attempt:
    """One delivery, kept whether it worked or not."""

    destination_id: str
    alert_id: str
    outcome: Outcome
    at: datetime
    attempts: int = 1
    status: int = 0
    detail: str = ""

    def to_dict(self) -> dict:
        return {"destination_id": self.destination_id, "alert_id": self.alert_id,
                "outcome": self.outcome.value, "at": self.at.isoformat(),
                "attempts": self.attempts, "status": self.status,
                "detail": self.detail}


def redact(alert: dict, base_url: str = "") -> dict:
    """The part of an alert that may leave the platform.

    Deliberately a whitelist. A blacklist fails open: the next field added to
    an alert -- a copilot summary, a coordinate, an evidence excerpt -- would
    be forwarded to every configured webhook by default, and nobody would
    notice until it was already in somebody's chat history.
    """
    payload = {k: v for k, v in alert.items() if k in ALLOWED_FIELDS}
    payload.setdefault("alert_id", alert.get("id", ""))
    payload.setdefault("platform", "terrashield")
    payload.setdefault("version", 1)
    if base_url and payload.get("alert_id"):
        payload["url"] = f"{base_url.rstrip('/')}/#/alerts"
    #: Said explicitly in the payload, so an integrator reading one delivery
    #: understands why it is so thin and goes to the platform for the rest.
    payload["note"] = ("references only; the finding, its evidence and its "
                       "imagery stay in TerraShield behind the audit log")
    return payload


def sign(body: bytes, secret: str, timestamp: int) -> str:
    """HMAC-SHA256 over timestamp and body, as `t=<ts>,v1=<hex>`.

    The timestamp is inside the signed material, not beside it. Signing only
    the body would let anyone who captured a valid delivery resend it forever
    with a fresh timestamp.
    """
    payload = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify(body: bytes, header: str, secret: str,
           now: int | None = None,
           window_s: int = REPLAY_WINDOW_S) -> bool:
    """What a receiver should run. Shipped so integrators do not invent it."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        timestamp = int(parts["t"])
        given = parts["v1"]
    except (ValueError, KeyError):
        return False
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > window_s:
        return False
    expected = sign(body, secret, timestamp).split("v1=")[1]
    return hmac.compare_digest(expected, given)


@dataclass
class Dispatcher:
    """Sends alerts to configured destinations, and records what happened."""

    destinations: list[Destination] = field(default_factory=list)
    transport: Transport = field(default_factory=UrllibTransport)
    base_url: str = ""
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time
    log: list[Attempt] = field(default_factory=list)

    def dispatch(self, alert: dict) -> list[Attempt]:
        """Send one alert everywhere it should go."""
        priority = int(alert.get("priority", 4) or 4)
        alert_id = alert.get("id") or alert.get("alert_id") or ""
        results: list[Attempt] = []

        for destination in self.destinations:
            if not destination.accepts(priority):
                attempt = Attempt(
                    destination.id, alert_id, Outcome.SUPPRESSED,
                    self._now(), attempts=0,
                    detail=("disabled" if not destination.enabled else
                            f"priority {priority} is below this "
                            f"destination's threshold of "
                            f"{destination.min_priority}"))
                self.log.append(attempt)
                results.append(attempt)
                continue
            attempt = self._send(destination, alert, alert_id)
            self.log.append(attempt)
            results.append(attempt)
        return results

    # -- internals ---------------------------------------------------------

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), tz=timezone.utc)

    def _send(self, destination: Destination, alert: dict,
              alert_id: str) -> Attempt:
        if destination.channel is Channel.EMAIL:
            #: No SMTP client here on purpose. Mail needs a relay, credentials
            #: and a deliverability story that belong to the deployment, not to
            #: this module -- and a half-working mailer that silently drops is
            #: the exact failure this file exists to prevent.
            return Attempt(
                destination.id, alert_id, Outcome.FAILED, self._now(),
                attempts=0,
                detail=("email delivery needs an SMTP relay configured for the "
                        "deployment; no mail is sent and none is claimed to be"))

        if not destination.secret:
            raise DeliveryError(
                f"webhook destination {destination.id} has no signing secret. "
                "An unsigned webhook cannot be told from anything else that "
                "can reach the endpoint")

        body = json.dumps(redact(alert, self.base_url),
                          separators=(",", ":"), sort_keys=True).encode()
        last = ""
        status = 0
        for attempt_no in range(1, MAX_ATTEMPTS + 1):
            timestamp = int(self.clock())
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "TerraShield/0.1",
                "X-TerraShield-Signature": sign(body, destination.secret,
                                                timestamp),
                "X-TerraShield-Delivery": f"{alert_id}:{attempt_no}",
            }
            try:
                response = self.transport.request(
                    "POST", destination.target, headers=headers, body=body,
                    timeout=10.0)
                status = response.status
                if 200 <= response.status < 300:
                    return Attempt(destination.id, alert_id, Outcome.DELIVERED,
                                   self._now(), attempt_no, response.status)
                last = f"HTTP {response.status}"
            except TransportError as e:
                status = e.status
                last = e.message
                #: A 4xx is the receiver saying the request is wrong. Repeating
                #: it produces the same answer and spends the endpoint's rate
                #: limit for nothing.
                if 400 <= e.status < 500 and e.status != 429:
                    break
            if attempt_no < MAX_ATTEMPTS:
                self.sleep(BACKOFF_BASE ** attempt_no)

        return Attempt(destination.id, alert_id, Outcome.FAILED, self._now(),
                       attempt_no, status,
                       last or "no response from the endpoint")

    # -- reporting ---------------------------------------------------------

    def summary(self) -> dict:
        """What an operator needs to see, with failures foregrounded.

        Silent failure is the risk: an alert that did not send looks exactly
        like a quiet week unless somebody is shown the difference.
        """
        delivered = [a for a in self.log if a.outcome is Outcome.DELIVERED]
        failed = [a for a in self.log if a.outcome is Outcome.FAILED]
        suppressed = [a for a in self.log if a.outcome is Outcome.SUPPRESSED]
        return {
            "delivered": len(delivered),
            "failed": len(failed),
            "suppressed": len(suppressed),
            "failures": [a.to_dict() for a in failed[-20:]],
            "note": ("a failed delivery means nobody was told. It is not the "
                     "same as a quiet period and is shown separately for that "
                     "reason"),
        }
