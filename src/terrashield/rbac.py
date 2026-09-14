"""Roles, permissions, and the audit record of who saw what.

PRD section 39 makes enterprise security mandatory, and for a government
customer the audit trail is not a feature alongside the analytics -- it is
often the thing that has to be demonstrated first.

Two choices here are worth stating.

**Permissions are strings in a table, not code.** A customer's security review
asks "who can download imagery" and the answer should be a table, readable
without a developer.

**Reading imagery is an audited act.** Most systems audit writes. In an
intelligence platform, the sensitive operation is usually a read: which sites
somebody looked at, and when, is exactly what an insider-threat review needs.
`AuditLog` therefore records image access and every AI interaction alongside
the mutations.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .domain import Role

#: Permission name -> the minimum role that holds it. Roles are cumulative.
PERMISSIONS: dict[str, Role] = {
    "alert.read": Role.VIEWER,
    "site.read": Role.VIEWER,
    "imagery.read": Role.VIEWER,
    "report.read": Role.VIEWER,
    "copilot.ask": Role.VIEWER,

    "aoi.create": Role.ANALYST,
    "aoi.update": Role.ANALYST,
    "rule.create": Role.ANALYST,
    "rule.update": Role.ANALYST,
    "finding.review": Role.ANALYST,
    "watchlist.manage": Role.ANALYST,
    "imagery.task": Role.ANALYST,      # spends money per square kilometre

    "finding.escalate": Role.SUPERVISOR,
    "report.publish": Role.SUPERVISOR,
    "evidence.export": Role.SUPERVISOR,   # data leaves the system

    "user.manage": Role.ADMIN,
    "org.configure": Role.ADMIN,
    "retention.configure": Role.ADMIN,
    "audit.read": Role.ADMIN,
}

_ORDER = [Role.VIEWER, Role.ANALYST, Role.SUPERVISOR, Role.ADMIN]


def rank(role: Role) -> int:
    return _ORDER.index(role)


def allows(role: Role, permission: str) -> bool:
    needed = PERMISSIONS.get(permission)
    if needed is None:
        #: Unknown permissions are denied, not allowed. A typo in a permission
        #: name should close a door, not open one.
        return False
    return rank(role) >= rank(needed)


def permissions_of(role: Role) -> list[str]:
    return sorted(p for p, needed in PERMISSIONS.items() if allows(role, p))


class AccessDenied(Exception):
    def __init__(self, actor: str, permission: str, role: Role):
        super().__init__(
            f"{actor} holds role {role.value}, which does not include "
            f"{permission!r}; it requires at least "
            f"{PERMISSIONS.get(permission, Role.ADMIN).value}")
        self.permission = permission
        self.role = role


def require(role: Role, permission: str, actor: str = "unknown") -> None:
    if not allows(role, permission):
        raise AccessDenied(actor, permission, role)


class TenantViolation(Exception):
    """Raised when a query would cross an organisation boundary.

    Its own exception type because it is the one bug class that must never be
    quietly handled. Everything else can be logged and recovered; this one is a
    breach, and it should be loud.
    """


def same_tenant(actor_org: str, resource_org: str, what: str = "resource") -> None:
    if actor_org != resource_org:
        raise TenantViolation(
            f"{what} belongs to organisation {resource_org}, the caller to "
            f"{actor_org}; cross-tenant access is refused")


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

@dataclass
class AuditEntry:
    """One recorded action, chained to the one before it.

    The chain is what makes the log worth having. Any log can be written to;
    a hash-chained log cannot be *edited* without breaking every entry after
    the edit, so "the audit trail was tampered with" becomes a detectable
    statement rather than a matter of trust.
    """

    seq: int
    at: datetime
    actor: str
    org_id: str
    action: str
    subject: str
    detail: dict = field(default_factory=dict)
    prev_hash: str = ""

    @property
    def hash(self) -> str:
        blob = json.dumps({
            "seq": self.seq, "at": self.at.isoformat(), "actor": self.actor,
            "org_id": self.org_id, "action": self.action,
            "subject": self.subject, "detail": self.detail,
            "prev": self.prev_hash,
        }, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()

    def to_dict(self) -> dict:
        return {
            "seq": self.seq, "at": self.at.isoformat(), "actor": self.actor,
            "org_id": self.org_id, "action": self.action,
            "subject": self.subject, "detail": self.detail,
            "prev_hash": self.prev_hash, "hash": self.hash,
        }


#: Actions recorded even though they change nothing. PRD section 39 lists
#: image access and AI interactions explicitly, and it is right to: in an
#: intelligence system, who looked at which site is more sensitive than who
#: edited a rule.
READ_ACTIONS = frozenset({
    "imagery.read", "evidence.export", "copilot.ask", "report.read",
})


def entry(seq: int, actor: str, org_id: str, action: str, subject: str,
          detail: dict | None = None, prev_hash: str = "") -> AuditEntry:
    return AuditEntry(seq=seq, at=datetime.now(timezone.utc), actor=actor,
                      org_id=org_id, action=action, subject=subject,
                      detail=detail or {}, prev_hash=prev_hash)


def verify_chain(entries: list[AuditEntry]) -> tuple[bool, int | None]:
    """Walk the chain. Returns (intact, sequence of the first bad entry)."""
    prev = ""
    for e in sorted(entries, key=lambda x: x.seq):
        if e.prev_hash != prev:
            return False, e.seq
        prev = e.hash
    return True, None
