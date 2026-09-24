"""Accounts, passwords and sessions.

The rest of the system is scoped to an organisation and an acting user, and
until now something outside it had to supply both. `api.serve` took a dictionary
mapping bearer tokens to identities, which is a fine seam for SSO and a
non-answer for "how does a customer sign up". This module is that answer.

Three decisions worth stating, because each is a place where a monitoring
platform leaks:

**Passwords are hashed with scrypt, per user, with a random salt.** scrypt is
memory-hard, which is the property that matters against an attacker with a GPU
and a copy of the table. The parameters are stored alongside each hash, so
raising them later does not invalidate existing passwords -- a login re-hashes
transparently when it sees a weaker record. `hashlib.scrypt` is in the standard
library, so this costs no dependency.

**Session tokens are stored as digests, never as tokens.** The server issues a
`secrets.token_urlsafe` value, keeps only its SHA-256, and forgets the original.
A stolen database therefore yields no usable sessions. This is the same reason
passwords are hashed and it is skipped surprisingly often.

**Every comparison is constant-time.** `hmac.compare_digest` throughout. Token
lookup is by digest, which is an index probe rather than a scan, so there is no
timing signal in the lookup either.

What this is not: an identity provider. There is no email verification, no
password reset, no MFA, no OAuth. A government deployment will federate to its
own directory, and `resolve_session` is the seam where that plugs in --
everything above it takes (org, actor, role) and does not care where they came
from. `docs/04-deploying-for-real.md` says what to replace.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .domain import Role

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

#: scrypt cost parameters. n is the work factor and dominates both cost and
#: memory: 2**15 with r=8 is about 32 MB and a few tens of milliseconds per
#: hash, which is the usual balance between "slow enough to be expensive to
#: attack" and "fast enough that a login does not feel broken".
#:
#: These are recorded in every stored hash rather than read from here at verify
#: time, so raising them is safe: existing records keep verifying under their
#: own parameters and are upgraded on the next successful login.
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32

#: Long enough to be worth hashing, short enough that a passphrase still fits
#: under any sane input limit. Length is the only requirement enforced: rules
#: demanding a digit and a symbol push people towards "Password1!" and are worse
#: than useless.
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024

_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


class AuthError(Exception):
    """Raised for anything a caller is allowed to see the reason for."""


def hash_password(password: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R,
                  p: int = SCRYPT_P) -> str:
    """Hash a password for storage. Self-describing, so parameters can move."""
    check_password_strength(password)
    salt = secrets.token_bytes(SALT_BYTES)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                         dklen=KEY_BYTES, maxmem=n * r * 256)
    return "$".join(("scrypt", str(n), str(r), str(p),
                     base64.b64encode(salt).decode(),
                     base64.b64encode(key).decode()))


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verify against a stored hash, under its own parameters."""
    try:
        scheme, n_s, r_s, p_s, salt_b64, key_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(key_b64)
    except (ValueError, TypeError):
        #: A malformed record is a failed login, not a crash. It is also worth
        #: nobody's time to distinguish "corrupt" from "wrong" to a caller.
        return False
    actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                            dklen=len(expected), maxmem=n * r * 256)
    return hmac.compare_digest(actual, expected)


def needs_rehash(stored: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R,
                 p: int = SCRYPT_P) -> bool:
    """True if this hash is weaker than current policy and should be upgraded."""
    try:
        scheme, n_s, r_s, p_s, _, _ = stored.split("$")
    except ValueError:
        return True
    return scheme != "scrypt" or (int(n_s), int(r_s), int(p_s)) < (n, r, p)


def check_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        #: Unbounded input into a deliberately slow hash is a denial of service
        #: with extra steps.
        raise AuthError(
            f"password must be at most {MAX_PASSWORD_LENGTH} characters")


def normalise_email(email: str) -> str:
    cleaned = email.strip().lower()
    if not _EMAIL.match(cleaned):
        raise AuthError(f"{email!r} is not a valid email address")
    return cleaned


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

#: How long a session lasts without being used again. Eight hours is a working
#: day: an analyst signs in once and is not interrupted, and a session left on
#: an unattended terminal overnight is gone by morning.
SESSION_TTL = timedelta(hours=8)

#: 32 bytes from `secrets` is 256 bits of entropy. Guessing is not the attack.
TOKEN_BYTES = 32


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_digest(token: str) -> str:
    """What gets stored. The token itself is never written anywhere."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Account:
    id: str
    org_id: str
    email: str
    name: str
    role: Role
    active: bool

    def to_dict(self) -> dict:
        return {"id": self.id, "org_id": self.org_id, "email": self.email,
                "name": self.name, "role": self.role.value,
                "active": self.active}


@dataclass(frozen=True)
class SessionInfo:
    token: str
    account: Account
    expires_at: datetime

    def to_dict(self) -> dict:
        return {"token": self.token, "expires_at": self.expires_at.isoformat(),
                "account": self.account.to_dict()}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dt(text: str) -> datetime:
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _account(row: sqlite3.Row) -> Account:
    return Account(id=row["id"], org_id=row["org_id"], email=row["email"],
                   name=row["name"], role=Role(row["role"]),
                   active=bool(row["active"]))


class Accounts:
    """Sign-up, sign-in and session resolution.

    Deliberately works on a raw connection rather than through `Store`, because
    `Store` is scoped to an organisation and an actor -- which is precisely what
    this module exists to establish. Asking it to authenticate would be circular.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row

    # -- registration ------------------------------------------------------

    def register_organisation(self, org_name: str, email: str, password: str,
                              name: str = "") -> Account:
        """Create an organisation and its first user, who is its administrator.

        One transaction. An organisation with no way to sign into it is worse
        than no organisation, so a failure here must leave neither.
        """
        email = normalise_email(email)
        org_name = org_name.strip()
        if not org_name:
            raise AuthError("an organisation name is required")
        if self.find_by_email(email) is not None:
            #: Deliberately the same message the API returns for a duplicate on
            #: any path. Whether an address is registered is not a secret worth
            #: a different code path, but it should not be phrased as a hint.
            raise AuthError("that email address is already registered")

        digest = hash_password(password)
        now = _now().isoformat()
        org_id = f"org-{uuid.uuid4().hex[:12]}"
        user_id = f"usr-{uuid.uuid4().hex[:12]}"
        with self.conn:
            self.conn.execute(
                "INSERT INTO organizations (id, name, created_at) VALUES (?,?,?)",
                (org_id, org_name, now))
            self.conn.execute(
                "INSERT INTO users (id, org_id, name, email, role, active, "
                "created_at, password_hash) VALUES (?,?,?,?,?,1,?,?)",
                (user_id, org_id, name.strip() or email, email,
                 Role.ADMIN.value, now, digest))
        return Account(user_id, org_id, email, name.strip() or email,
                       Role.ADMIN, True)

    def add_user(self, org_id: str, email: str, password: str,
                 role: Role = Role.ANALYST, name: str = "") -> Account:
        """Add a user to an existing organisation."""
        email = normalise_email(email)
        if self.find_by_email(email) is not None:
            raise AuthError("that email address is already registered")
        org = self.conn.execute("SELECT id FROM organizations WHERE id = ?",
                                (org_id,)).fetchone()
        if org is None:
            raise AuthError("no such organisation")

        digest = hash_password(password)
        now = _now().isoformat()
        user_id = f"usr-{uuid.uuid4().hex[:12]}"
        with self.conn:
            self.conn.execute(
                "INSERT INTO users (id, org_id, name, email, role, active, "
                "created_at, password_hash) VALUES (?,?,?,?,?,1,?,?)",
                (user_id, org_id, name.strip() or email, email, role.value,
                 now, digest))
        return Account(user_id, org_id, email, name.strip() or email, role, True)

    def find_by_email(self, email: str) -> Account | None:
        """Look up across every organisation. Email is the global identifier."""
        row = self.conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.strip().lower(),)
        ).fetchone()
        return _account(row) if row else None

    # -- authentication ----------------------------------------------------

    def authenticate(self, email: str, password: str) -> Account:
        """Verify a password. Raises `AuthError` with one message for any failure.

        A wrong address and a wrong password are the same answer on purpose. The
        work is also done either way: when no user matches, a dummy verify runs
        so the response time does not say whether the address exists.
        """
        try:
            cleaned = normalise_email(email)
        except AuthError:
            cleaned = ""
        row = self.conn.execute(
            "SELECT * FROM users WHERE email = ?", (cleaned,)).fetchone() \
            if cleaned else None

        if row is None:
            verify_password(password, _DUMMY_HASH)
            raise AuthError("email or password is incorrect")
        if not verify_password(password, row["password_hash"] or ""):
            raise AuthError("email or password is incorrect")
        if not row["active"]:
            #: Distinguished from a bad password: the credentials were right,
            #: and telling someone their account is disabled saves a support
            #: ticket without telling an attacker anything they could not learn
            #: by having the password already.
            raise AuthError("this account is disabled")

        if needs_rehash(row["password_hash"] or ""):
            with self.conn:
                self.conn.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (hash_password(password), row["id"]))
        with self.conn:
            self.conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?",
                              (_now().isoformat(), row["id"]))
        return _account(row)

    def sign_in(self, email: str, password: str,
                ttl: timedelta = SESSION_TTL) -> SessionInfo:
        """Authenticate and open a session. The token is returned exactly once."""
        account = self.authenticate(email, password)
        return self.open_session(account, ttl=ttl)

    # -- sessions ----------------------------------------------------------

    def open_session(self, account: Account,
                     ttl: timedelta = SESSION_TTL) -> SessionInfo:
        token = new_token()
        now = _now()
        expires = now + ttl
        with self.conn:
            self.conn.execute(
                "INSERT INTO sessions (token_hash, user_id, org_id, created_at,"
                " expires_at, revoked_at) VALUES (?,?,?,?,?,'')",
                (token_digest(token), account.id, account.org_id,
                 now.isoformat(), expires.isoformat()))
        return SessionInfo(token=token, account=account, expires_at=expires)

    def resolve_session(self, token: str) -> Account | None:
        """The identity behind a bearer token, or None.

        This is the seam an external identity provider replaces: everything
        above it wants (org, actor, role) and does not care how they were
        established.
        """
        if not token:
            return None
        row = self.conn.execute(
            "SELECT s.expires_at, s.revoked_at, u.* FROM sessions s "
            "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
            (token_digest(token),)).fetchone()
        if row is None or row["revoked_at"]:
            return None
        if _dt(row["expires_at"]) <= _now():
            return None
        if not row["active"]:
            #: Disabling a user has to take effect on their open sessions too,
            #: or "disabled" means "disabled at next sign-in", which is not what
            #: anyone means by it.
            return None
        return _account(row)

    def revoke_session(self, token: str) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE sessions SET revoked_at = ? "
                "WHERE token_hash = ? AND revoked_at = ''",
                (_now().isoformat(), token_digest(token)))
        return cur.rowcount > 0

    def revoke_all_for_user(self, user_id: str) -> int:
        """Every session for one user. What a password change must do."""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE sessions SET revoked_at = ? "
                "WHERE user_id = ? AND revoked_at = ''",
                (_now().isoformat(), user_id))
        return cur.rowcount

    def purge_expired(self) -> int:
        with self.conn:
            cur = self.conn.execute("DELETE FROM sessions WHERE expires_at <= ?",
                                    (_now().isoformat(),))
        return cur.rowcount

    # -- maintenance -------------------------------------------------------

    def change_password(self, account: Account, current: str, new: str) -> None:
        """Change a password, and end every session it could have opened."""
        self.authenticate(account.email, current)
        digest = hash_password(new)
        with self.conn:
            self.conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                              (digest, account.id))
        self.revoke_all_for_user(account.id)

    def set_active(self, user_id: str, active: bool) -> None:
        with self.conn:
            self.conn.execute("UPDATE users SET active = ? WHERE id = ?",
                              (1 if active else 0, user_id))
        if not active:
            self.revoke_all_for_user(user_id)

    def list_users(self, org_id: str) -> list[Account]:
        rows = self.conn.execute(
            "SELECT * FROM users WHERE org_id = ? ORDER BY created_at",
            (org_id,)).fetchall()
        return [_account(r) for r in rows]


#: A real hash of a value nobody knows, used to spend the same time on a login
#: for an address that does not exist. Generated once at import.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(32))
