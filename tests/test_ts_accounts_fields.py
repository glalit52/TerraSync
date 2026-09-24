"""Accounts, sessions and fields.

Two things this suite is careful about, because both are places where a
monitoring platform leaks quietly rather than loudly:

* a stolen database must not yield usable sessions or recoverable passwords;
* a field drawn outside its AOI must be refused at creation, because imagery is
  only fetched for the AOI footprint and such a field would look monitored
  forever without ever producing a finding.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from terrashield.accounts import (
    Accounts, AuthError, hash_password, needs_rehash, token_digest,
    verify_password,
)
from terrashield.domain import Aoi, AoiKind, Field, Role
from terrashield.geo import rectangle
from terrashield.store import Store
from terrashield.store.repo import StoreError
from terrashield.store.schema import migrate

GOOD = "correct-horse-battery-staple"


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "accounts.db")
    migrate(c)
    return c


@pytest.fixture
def accounts(conn):
    return Accounts(conn)


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

def test_a_password_is_never_recoverable_from_its_hash():
    stored = hash_password(GOOD)
    assert GOOD not in stored
    assert stored.startswith("scrypt$")
    assert verify_password(GOOD, stored)
    assert not verify_password(GOOD + "x", stored)


def test_the_same_password_hashes_differently_every_time():
    """Per-user salt. Two people with the same password must not collide."""
    assert hash_password(GOOD) != hash_password(GOOD)


def test_a_weak_password_is_refused():
    with pytest.raises(AuthError, match="at least"):
        hash_password("short")


def test_an_unbounded_password_is_refused():
    """Arbitrary input into a deliberately slow hash is a denial of service."""
    with pytest.raises(AuthError, match="at most"):
        hash_password("x" * 5000)


def test_a_malformed_hash_fails_the_login_rather_than_crashing():
    for junk in ("", "garbage", "scrypt$notanumber$8$1$aaaa$bbbb", "md5$x$y"):
        assert verify_password(GOOD, junk) is False


def test_weaker_stored_parameters_are_flagged_for_upgrade():
    weak = hash_password(GOOD, n=1 << 14)
    assert needs_rehash(weak)
    assert not needs_rehash(hash_password(GOOD))
    #: and the weak one still verifies, under its own parameters
    assert verify_password(GOOD, weak)


# ---------------------------------------------------------------------------
# Sign-up and sign-in
# ---------------------------------------------------------------------------

def test_signing_up_creates_an_organisation_and_an_administrator(accounts):
    account = accounts.register_organisation("Acme", "  Owner@Acme.COM ", GOOD)
    assert account.email == "owner@acme.com", "normalised"
    assert account.role is Role.ADMIN
    assert account.org_id and account.id


def test_an_email_can_only_be_registered_once(accounts):
    accounts.register_organisation("Acme", "owner@acme.com", GOOD)
    with pytest.raises(AuthError, match="already registered"):
        accounts.register_organisation("Other", "owner@acme.com", GOOD)
    with pytest.raises(AuthError, match="already registered"):
        accounts.add_user(accounts.find_by_email("owner@acme.com").org_id,
                          "OWNER@acme.com", GOOD)


def test_a_wrong_password_and_an_unknown_address_answer_identically(accounts):
    """Login must not be an oracle for which addresses are registered."""
    accounts.register_organisation("Acme", "owner@acme.com", GOOD)
    messages = set()
    for email, password in (("owner@acme.com", "wrong-password-entirely"),
                            ("nobody@acme.com", GOOD),
                            ("not-an-email", GOOD)):
        with pytest.raises(AuthError) as caught:
            accounts.authenticate(email, password)
        messages.add(str(caught.value))
    assert messages == {"email or password is incorrect"}


def test_an_invalid_email_is_refused_at_signup(accounts):
    with pytest.raises(AuthError, match="not a valid email"):
        accounts.register_organisation("Acme", "not-an-email", GOOD)


def test_an_organisation_needs_a_name(accounts):
    with pytest.raises(AuthError, match="organisation name"):
        accounts.register_organisation("   ", "owner@acme.com", GOOD)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def test_the_session_token_is_never_stored(accounts, conn):
    accounts.register_organisation("Acme", "owner@acme.com", GOOD)
    session = accounts.sign_in("owner@acme.com", GOOD)

    stored = [r[0] for r in conn.execute("SELECT token_hash FROM sessions")]
    assert stored == [token_digest(session.token)]
    assert session.token not in stored[0]
    #: the whole database, not just that column
    dump = "\n".join(conn.iterdump())
    assert session.token not in dump


def test_a_session_resolves_to_its_account(accounts):
    accounts.register_organisation("Acme", "owner@acme.com", GOOD)
    session = accounts.sign_in("owner@acme.com", GOOD)
    resolved = accounts.resolve_session(session.token)
    assert resolved is not None
    assert resolved.email == "owner@acme.com"
    assert resolved.role is Role.ADMIN


def test_an_unknown_or_empty_token_resolves_to_nothing(accounts):
    assert accounts.resolve_session("") is None
    assert accounts.resolve_session("not-a-real-token") is None


def test_a_revoked_session_stops_working(accounts):
    accounts.register_organisation("Acme", "owner@acme.com", GOOD)
    session = accounts.sign_in("owner@acme.com", GOOD)
    assert accounts.revoke_session(session.token) is True
    assert accounts.resolve_session(session.token) is None
    assert accounts.revoke_session(session.token) is False, "already revoked"


def test_an_expired_session_stops_working(accounts):
    accounts.register_organisation("Acme", "owner@acme.com", GOOD)
    session = accounts.sign_in("owner@acme.com", GOOD,
                               ttl=timedelta(seconds=-1))
    assert accounts.resolve_session(session.token) is None


def test_disabling_a_user_ends_the_sessions_they_already_had(accounts):
    """Otherwise 'disabled' quietly means 'disabled at next sign-in'."""
    owner = accounts.register_organisation("Acme", "owner@acme.com", GOOD)
    analyst = accounts.add_user(owner.org_id, "a@acme.com", GOOD, Role.ANALYST)
    session = accounts.sign_in("a@acme.com", GOOD)
    assert accounts.resolve_session(session.token) is not None

    accounts.set_active(analyst.id, False)
    assert accounts.resolve_session(session.token) is None
    with pytest.raises(AuthError, match="disabled"):
        accounts.authenticate("a@acme.com", GOOD)


def test_changing_a_password_ends_every_session_it_could_have_opened(accounts):
    owner = accounts.register_organisation("Acme", "owner@acme.com", GOOD)
    first = accounts.sign_in("owner@acme.com", GOOD)
    second = accounts.sign_in("owner@acme.com", GOOD)

    accounts.change_password(owner, GOOD, "an-entirely-new-passphrase")
    assert accounts.resolve_session(first.token) is None
    assert accounts.resolve_session(second.token) is None
    assert accounts.authenticate("owner@acme.com", "an-entirely-new-passphrase")


def test_changing_a_password_requires_the_current_one(accounts):
    owner = accounts.register_organisation("Acme", "owner@acme.com", GOOD)
    with pytest.raises(AuthError):
        accounts.change_password(owner, "not-the-current-one", "a-new-passphrase")


def test_expired_sessions_can_be_purged(accounts, conn):
    accounts.register_organisation("Acme", "owner@acme.com", GOOD)
    accounts.sign_in("owner@acme.com", GOOD, ttl=timedelta(seconds=-1))
    live = accounts.sign_in("owner@acme.com", GOOD)
    assert accounts.purge_expired() == 1
    assert accounts.resolve_session(live.token) is not None


# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------

ORG = "org-fields"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "fields.db", org_id=ORG, actor="a@x.example",
              role=Role.ADMIN)
    s.conn.execute("INSERT INTO organizations (id, name, created_at) "
                   "VALUES (?,?,?)", (ORG, "Fields", "2026-01-01T00:00:00+00:00"))
    s.conn.commit()
    s.put_aoi(Aoi("AOI-1", ORG, "Farm", rectangle((73.0, 21.0), 4000, 4000),
                  AoiKind.GENERIC))
    return s


def _field(fid, name, centre=(73.0, 21.0), size=600, aoi="AOI-1", **kw):
    return Field(fid, ORG, aoi, name, rectangle(centre, size, size), **kw)


def test_a_field_is_stored_and_read_back(store):
    store.put_field(_field("f1", "Block D", use="wheat", notes="north slope"))
    back = store.get_field("f1")
    assert back is not None
    assert (back.name, back.use, back.notes) == ("Block D", "wheat", "north slope")
    assert back.area_hectares == pytest.approx(36.0, rel=1e-3)


def test_a_field_outside_its_aoi_is_refused(store):
    """Imagery is only fetched for the AOI footprint, so it would never be seen."""
    with pytest.raises(StoreError, match="outside AOI"):
        store.put_field(_field("f2", "Elsewhere", centre=(73.4, 21.4)))


def test_a_field_on_an_unknown_aoi_is_refused(store):
    with pytest.raises(StoreError, match="does not exist"):
        store.put_field(_field("f3", "Orphan", aoi="NO-SUCH-AOI"))


def test_a_field_needs_a_name(store):
    with pytest.raises(StoreError, match="needs a name"):
        store.put_field(_field("f4", "   "))


def test_fields_are_listed_per_aoi_and_scoped_to_the_tenant(store, tmp_path):
    store.put_field(_field("f1", "Block D", centre=(73.004, 21.004)))
    store.put_field(_field("f2", "Block E", centre=(72.996, 20.996)))
    assert [f.name for f in store.list_fields("AOI-1")] == ["Block D", "Block E"]

    other = Store(store.path, org_id="org-other", actor="x@y.example",
                  role=Role.ADMIN)
    assert other.list_fields("AOI-1") == []
    assert other.get_field("f1") is None


def test_a_point_resolves_to_the_field_it_falls_in(store):
    """This is what turns a change centroid into 'Block D'."""
    store.put_field(_field("f1", "Block D", centre=(73.004, 21.004)))
    store.put_field(_field("f2", "Block E", centre=(72.996, 20.996)))

    assert store.field_at(73.004, 21.004).name == "Block D"
    assert store.field_at(72.996, 20.996).name == "Block E"
    assert store.field_at(73.012, 21.012) is None, "inside the AOI, in no field"


def test_a_retired_field_stops_being_listed_but_is_still_resolvable(store):
    """A bundle that names a field nobody can look up fails its own audit."""
    store.put_field(_field("f1", "Block D"))
    assert store.deactivate_field("f1") is True
    assert store.list_fields("AOI-1") == []
    assert store.get_field("f1") is not None
    assert store.field_at(73.0, 21.0) is None, "retired fields stop matching"


def test_field_changes_are_audited(store):
    store.put_field(_field("f1", "Block D"))
    store.deactivate_field("f1")
    actions = [r["action"] for r in
               store.conn.execute("SELECT action FROM audit_log")]
    assert "field.upsert" in actions
    assert "field.deactivate" in actions


def test_moving_a_field_changes_its_fingerprint(store):
    """The same reason an AOI's does: history built on the old shape is not
    history of the new one."""
    first = _field("f1", "Block D", centre=(73.002, 21.002))
    moved = _field("f1", "Block D", centre=(73.003, 21.003))
    assert first.fingerprint != moved.fingerprint


def test_an_analyst_may_manage_fields_but_a_viewer_may_not(store):
    viewer = Store(store.path, org_id=ORG, actor="v@x.example", role=Role.VIEWER)
    with pytest.raises(Exception):
        viewer.put_field(_field("f9", "Nope"))

    analyst = Store(store.path, org_id=ORG, actor="an@x.example",
                    role=Role.ANALYST)
    analyst.put_field(_field("f8", "Allowed"))
    assert analyst.get_field("f8") is not None
