"""The HTTP surface: signing up, signing in, and managing areas and fields.

Driven through `dispatch` and `dispatch_public` rather than a live socket, so
the suite stays fast and deterministic. The one thing that genuinely needs a
socket -- that an unauthenticated request never reaches a handler -- is asserted
here through the same code path the handler uses.
"""

from __future__ import annotations

import sqlite3

import pytest

from terrashield.accounts import Accounts
from terrashield.api import dispatch, dispatch_public
from terrashield.domain import Role
from terrashield.store import Store
from terrashield.store.schema import migrate

GOOD = "correct-horse-battery-staple"

SQUARE = {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[
    [72.98, 20.98], [73.02, 20.98], [73.02, 21.02], [72.98, 21.02],
    [72.98, 20.98]]]}}
INNER = [[72.99, 20.99], [73.00, 20.99], [73.00, 21.00], [72.99, 21.00],
         [72.99, 20.99]]
FAR_AWAY = [[74.5, 22.5], [74.6, 22.5], [74.6, 22.6], [74.5, 22.6], [74.5, 22.5]]


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "api.db")


@pytest.fixture
def accounts(db):
    conn = sqlite3.connect(db)
    migrate(conn)
    return Accounts(conn)


def public(accounts, method, path, body=None, token="", allow_signup=True):
    """Most of these tests exercise registration, so it is open by default.

    The tests that care about it being *closed* pass the flag explicitly, and
    live next to the ones asserting that signing in still works when it is.
    """
    return dispatch_public(accounts, method, path, {}, body or {}, token,
                           allow_signup)


@pytest.fixture
def signed_in(accounts, db):
    status, payload = public(accounts, "POST", "/api/auth/signup", {
        "organisation": "Sensegrass", "email": "owner@example.com",
        "password": GOOD})
    assert status == 200, payload
    account = accounts.resolve_session(payload["token"])
    store = Store(db, org_id=account.org_id, actor=account.email,
                  role=account.role)
    return payload["token"], store


def call(store, method, path, body=None, params=None):
    return dispatch(store, method, path, params or {}, body or {})


# ---------------------------------------------------------------------------
# Sign-up and sign-in
# ---------------------------------------------------------------------------

def test_signup_creates_an_organisation_and_returns_a_session(accounts):
    status, payload = public(accounts, "POST", "/api/auth/signup", {
        "organisation": "Sensegrass", "email": "owner@example.com",
        "password": GOOD})
    assert status == 200
    assert payload["account"]["role"] == Role.ADMIN.value
    assert accounts.resolve_session(payload["token"]) is not None


def test_signup_requires_its_fields(accounts):
    for missing in ("organisation", "email", "password"):
        body = {"organisation": "S", "email": "a@b.com", "password": GOOD}
        del body[missing]
        status, payload = public(accounts, "POST", "/api/auth/signup", body)
        assert status == 400
        assert missing in payload["error"]


def test_signup_rejects_a_weak_password(accounts):
    status, payload = public(accounts, "POST", "/api/auth/signup", {
        "organisation": "S", "email": "a@b.com", "password": "short"})
    assert status == 400
    assert "at least" in payload["error"]


def test_login_returns_401_identically_for_every_failure(accounts):
    public(accounts, "POST", "/api/auth/signup", {
        "organisation": "S", "email": "owner@example.com", "password": GOOD})

    seen = set()
    for email, password in (("owner@example.com", "wrong-password-here"),
                            ("nobody@example.com", GOOD)):
        status, payload = public(accounts, "POST", "/api/auth/login",
                                 {"email": email, "password": password})
        seen.add((status, payload["error"]))
    assert len(seen) == 1, "status and message must not distinguish the cases"
    assert seen.pop()[0] == 401


def test_logout_revokes_the_session(accounts):
    status, payload = public(accounts, "POST", "/api/auth/signup", {
        "organisation": "S", "email": "owner@example.com", "password": GOOD})
    token = payload["token"]
    assert accounts.resolve_session(token) is not None

    status, payload = public(accounts, "POST", "/api/auth/logout", {}, token)
    assert (status, payload) == (200, {"revoked": True})
    assert accounts.resolve_session(token) is None


def test_signing_in_works_when_registration_is_closed(accounts):
    """Login and registration are different things.

    Gating them together made the whole application unreachable on any server
    that had sensibly disabled self-service sign-up: an administrator could
    create accounts with `terrashield register`, and then nobody could use
    them. Only registration is a way *in*; signing in is how someone who
    already has an account uses the product.
    """
    public(accounts, "POST", "/api/auth/signup", {
        "organisation": "S", "email": "owner@example.com", "password": GOOD})

    status, payload = dispatch_public(
        accounts, "POST", "/api/auth/login", {},
        {"email": "owner@example.com", "password": GOOD}, "",
        allow_signup=False)
    assert status == 200, payload
    assert payload["token"]


def test_registration_is_refused_when_closed_and_names_the_alternative(accounts):
    status, payload = dispatch_public(
        accounts, "POST", "/api/auth/signup", {},
        {"organisation": "S", "email": "a@b.com", "password": GOOD}, "",
        allow_signup=False)
    assert status == 403
    assert "disabled" in payload["error"]
    assert "terrashield register" in payload["hint"]


def test_logging_out_works_when_registration_is_closed(accounts):
    _, payload = public(accounts, "POST", "/api/auth/signup", {
        "organisation": "S", "email": "owner@example.com", "password": GOOD})
    status, out = dispatch_public(
        accounts, "POST", "/api/auth/logout", {}, {}, payload["token"],
        allow_signup=False)
    assert (status, out) == (200, {"revoked": True})


def test_only_the_auth_routes_are_reachable_without_a_token(accounts):
    """The unauthenticated surface is a list you can read, not a property."""
    for path in ("/api/sites", "/api/alerts", "/api/me", "/api/audit",
                 "/api/nope"):
        status, _ = public(accounts, "GET", path)
        assert status == 0, f"{path} must fall through to authentication"


# ---------------------------------------------------------------------------
# Areas
# ---------------------------------------------------------------------------

def test_an_area_can_be_created_from_geojson(signed_in):
    _, store = signed_in
    status, payload = call(store, "POST", "/api/sites",
                           {"name": "Mundra Farm", "boundary": SQUARE})
    assert status == 200
    assert payload["site"]["area_km2"] > 0
    assert payload["site"]["name"] == "Mundra Farm"
    assert store.get_aoi(payload["site"]["id"]) is not None


def test_an_area_can_be_created_from_a_bare_ring(signed_in):
    """A drawing tool sends GeoJSON; a script sends pairs. Both must work."""
    _, store = signed_in
    status, payload = call(store, "POST", "/api/sites",
                           {"name": "By ring", "boundary": INNER})
    assert status == 200, payload
    assert payload["site"]["area_km2"] > 0


def test_creating_an_area_validates_its_geometry(signed_in):
    _, store = signed_in
    status, payload = call(store, "POST", "/api/sites",
                           {"name": "Degenerate", "boundary": [[1, 1], [2, 2]]})
    assert status == 400
    assert "error" in payload


def test_creating_an_area_requires_a_name_and_a_boundary(signed_in):
    _, store = signed_in
    assert call(store, "POST", "/api/sites", {"boundary": SQUARE})[0] == 400
    assert call(store, "POST", "/api/sites", {"name": "No shape"})[0] == 400


def test_an_unknown_area_kind_names_the_valid_ones(signed_in):
    _, store = signed_in
    status, payload = call(store, "POST", "/api/sites",
                           {"name": "X", "kind": "spaceport", "boundary": SQUARE})
    assert status == 400
    assert "spaceport" in payload["error"]
    assert "port" in payload["hint"]


# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------

@pytest.fixture
def with_area(signed_in):
    token, store = signed_in
    aoi_id = call(store, "POST", "/api/sites",
                  {"name": "Mundra Farm", "boundary": SQUARE})[1]["site"]["id"]
    return store, aoi_id


def test_a_field_can_be_added_to_an_area(with_area):
    store, aoi_id = with_area
    status, payload = call(store, "POST", f"/api/sites/{aoi_id}/fields",
                           {"name": "Block D", "use": "wheat",
                            "boundary": INNER})
    assert status == 200, payload
    assert payload["field"]["name"] == "Block D"
    assert payload["field"]["hectares"] > 0


def test_a_field_outside_its_area_is_refused_with_the_reason(with_area):
    store, aoi_id = with_area
    status, payload = call(store, "POST", f"/api/sites/{aoi_id}/fields",
                           {"name": "Elsewhere", "boundary": FAR_AWAY})
    assert status == 400
    assert "outside AOI" in payload["error"]


def test_fields_are_listed_with_their_total_area(with_area):
    store, aoi_id = with_area
    call(store, "POST", f"/api/sites/{aoi_id}/fields",
         {"name": "Block D", "boundary": INNER})
    status, payload = call(store, "GET", f"/api/sites/{aoi_id}/fields")
    assert status == 200
    assert payload["count"] == 1
    #: the summary rounds to two places and the item to three, so compare
    #: them at the coarser of the two rather than pretending otherwise.
    assert payload["hectares"] == pytest.approx(
        payload["fields"][0]["hectares"], abs=0.01)


def test_listing_fields_of_an_unknown_area_is_404(signed_in):
    _, store = signed_in
    assert call(store, "GET", "/api/sites/no-such-aoi/fields")[0] == 404


def test_a_coordinate_resolves_to_its_field(with_area):
    store, aoi_id = with_area
    call(store, "POST", f"/api/sites/{aoi_id}/fields",
         {"name": "Block D", "boundary": INNER})

    status, payload = call(store, "GET", "/api/fields/at", params={
        "lon": ["72.995"], "lat": ["20.995"]})
    assert status == 200
    assert payload["field"]["name"] == "Block D"

    status, payload = call(store, "GET", "/api/fields/at", params={
        "lon": ["73.015"], "lat": ["21.015"]})
    assert payload["field"] is None, "inside the area, in no field"


def test_a_coordinate_lookup_needs_numbers(signed_in):
    _, store = signed_in
    assert call(store, "GET", "/api/fields/at")[0] == 400
    assert call(store, "GET", "/api/fields/at",
                params={"lon": ["x"], "lat": ["1"]})[0] == 400


def test_a_field_can_be_retired(with_area):
    store, aoi_id = with_area
    field_id = call(store, "POST", f"/api/sites/{aoi_id}/fields",
                    {"name": "Block D", "boundary": INNER})[1]["field"]["id"]

    assert call(store, "POST", f"/api/fields/{field_id}/retire")[0] == 200
    assert call(store, "GET", f"/api/sites/{aoi_id}/fields")[1]["count"] == 0
    status, payload = call(store, "POST", f"/api/fields/{field_id}/retire")
    assert (status, payload["already_retired"]) == (200, True), "idempotent"
    assert call(store, "POST", "/api/fields/no-such-field/retire")[0] == 404


def test_a_viewer_cannot_create_areas_or_fields(signed_in, db):
    _, owner = signed_in
    aoi_id = call(owner, "POST", "/api/sites",
                  {"name": "Farm", "boundary": SQUARE})[1]["site"]["id"]

    viewer = Store(db, org_id=owner.org_id, actor="v@example.com",
                   role=Role.VIEWER)
    assert call(viewer, "POST", "/api/sites",
                {"name": "Nope", "boundary": SQUARE})[0] == 403
    assert call(viewer, "POST", f"/api/sites/{aoi_id}/fields",
                {"name": "Nope", "boundary": INNER})[0] == 403
    assert call(viewer, "GET", f"/api/sites/{aoi_id}/fields")[0] == 200


def test_areas_and_fields_are_scoped_to_the_tenant(signed_in, db):
    _, owner = signed_in
    aoi_id = call(owner, "POST", "/api/sites",
                  {"name": "Farm", "boundary": SQUARE})[1]["site"]["id"]
    call(owner, "POST", f"/api/sites/{aoi_id}/fields",
         {"name": "Block D", "boundary": INNER})

    other = Store(db, org_id="org-someone-else", actor="x@example.com",
                  role=Role.ADMIN)
    assert call(other, "GET", "/api/sites")[1]["sites"] == []
    assert call(other, "GET", f"/api/sites/{aoi_id}/fields")[0] == 404


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------

def test_serving_with_no_accounts_is_refused_even_with_signup_enabled(
        tmp_path, monkeypatch):
    """Open registration is a way in, not a substitute for having one.

    A server with no accounts and self-service sign-up is a server anyone who
    reaches the port can enrol on, which is the same failure as serving
    unauthenticated by a slightly longer route.
    """
    from terrashield.api import serve
    monkeypatch.delenv("TERRASHIELD_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="refusing to serve"):
        serve(str(tmp_path / "empty.db"), allow_signup=True)


def test_signup_is_off_unless_it_is_asked_for(tmp_path, monkeypatch, db,
                                              accounts):
    """The route exists; whether it is reachable is a deployment decision."""
    import inspect
    from terrashield.api import make_handler, serve

    assert inspect.signature(serve).parameters["allow_signup"].default is False

    #: make_handler is given an accounts factory only when signup is allowed,
    #: which is what gates the public routes.
    handler = make_handler(lambda token: None)
    assert handler is not None
