"""HTTP API — the endpoints in PRD section 40, on the standard library.

No framework, on purpose. The whole package has no dependencies, and that is
what makes an on-premise or air-gapped install a copy rather than a
procurement. It is a real API -- routing, typed handlers, JSON errors, bearer
auth, per-request identity -- but it is a single-process server, so put it
behind a proper ASGI stack and a TLS terminator before it faces a network.

Identity is per request and it matters here more than in most APIs. The token
resolves to a user, the user carries a role and an organisation, and the store
is opened *as that user*, so tenant isolation and permissions are enforced in
SQL rather than in these handlers. A missing filter in a handler below cannot
leak another organisation's sites, because the handler never gets to choose the
organisation.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import traceback
import uuid
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from . import copilot
from .accounts import Accounts, AuthError
from .domain import Aoi, AoiKind, Field, ReviewStatus, Role
from .geo import rings_from_geojson
from .pipeline import health
from .rbac import AccessDenied, TenantViolation
from .store.repo import Store, StoreError
from .store.schema import migrate

Handler = Callable[..., Any]
_ROUTES: list[tuple[str, re.Pattern, Handler]] = []


def route(method: str, pattern: str):
    """Register a handler. `{name}` in the pattern becomes a keyword argument."""
    regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

    def wrap(fn: Handler) -> Handler:
        _ROUTES.append((method, regex, fn))
        return fn
    return wrap


#: Routes that run *before* an identity exists, because they are how one is
#: established. Everything else goes through `store_factory` and is refused
#: without a valid bearer token; these four get an `Accounts` instead and are
#: the only unauthenticated surface in the API. Keeping them in a separate
#: registry means "what can be reached without signing in" is a list you can
#: read, rather than a property you have to derive from the routing table.
_PUBLIC_ROUTES: list[tuple[str, "re.Pattern[str]", Callable]] = []


def public_route(method: str, pattern: str):
    regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

    def wrap(fn):
        _PUBLIC_ROUTES.append((method, regex, fn))
        return fn
    return wrap


class ApiError(Exception):
    def __init__(self, status: int, message: str, hint: str = ""):
        super().__init__(message)
        self.status = status
        self.message = message
        self.hint = hint


def _date(params: dict, key: str, default: date | None = None) -> date | None:
    raw = params.get(key)
    if not raw:
        return default
    try:
        return date.fromisoformat(raw[0] if isinstance(raw, list) else raw)
    except ValueError:
        raise ApiError(400, f"{key} must be an ISO date such as 2026-05-01")


def _body_date(body: dict, key: str, default: date) -> date:
    """A date from a request body, or a 400 saying what was wrong with it.

    Parsing this inline let a typo in a caller's JSON raise ValueError, which
    the dispatcher turned into a 500 with a stack trace — an internal error for
    something the client got wrong, and a traceback handed to whoever asked.
    """
    raw = body.get(key)
    if raw in (None, ""):
        return default
    try:
        return date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        raise ApiError(400, f"{key!r} must be an ISO date such as 2026-05-01",
                       f"got {raw!r}")


def _one(params: dict, key: str, default: str = "") -> str:
    raw = params.get(key)
    if not raw:
        return default
    return raw[0] if isinstance(raw, list) else raw


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

@route("GET", "/api/health")
def api_health(store: Store, **_) -> dict:
    return health(store)

@route("GET", "/api/me")
def api_me(store: Store, **_) -> dict:
    from .rbac import permissions_of
    return {"actor": store.actor, "organisation": store.org_id,
            "role": store.role.value, "permissions": permissions_of(store.role)}


@route("GET", "/api/sites")
def api_sites(store: Store, **_) -> dict:
    return {"sites": [
        {"id": a.id, "name": a.name, "kind": a.kind.value, "country": a.country,
         "area_km2": round(a.area_km2, 2), "centroid": list(a.centroid),
         "bbox": list(a.bbox), "fingerprint": a.fingerprint,
         "description": a.description}
        for a in store.list_aois()]}


@route("GET", "/api/sites/{aoi_id}")
def api_site(store: Store, aoi_id: str, params: dict, **_) -> dict:
    aoi = store.get_aoi(aoi_id)
    if aoi is None:
        raise ApiError(404, f"no monitored area {aoi_id}")
    end = _date(params, "to", datetime.now(timezone.utc).date())
    start = _date(params, "from", end - timedelta(days=90))
    scenes = store.list_scenes(aoi_id, start, end)
    changes = store.list_changes(aoi_id, start=start, end=end)
    anomalies = store.list_anomalies(aoi_id, limit=200)
    return {
        "site": {"id": aoi.id, "name": aoi.name, "kind": aoi.kind.value,
                 "area_km2": round(aoi.area_km2, 2),
                 "boundary": [list(p) for p in aoi.boundary],
                 "fingerprint": aoi.fingerprint},
        "period": {"from": start.isoformat(), "to": end.isoformat()},
        "coverage": {"acquired": len(scenes),
                     "usable": len([s for s in scenes if s.usable])},
        "changes": len(changes),
        "peak_anomaly_score": round(max((a.score for a in anomalies), default=0.0), 1),
        "alerts": len(store.list_alerts(aoi_id, limit=200)),
    }


@route("GET", "/api/imagery")
def api_imagery(store: Store, params: dict, **_) -> dict:
    aoi_id = _one(params, "site")
    if not aoi_id:
        raise ApiError(400, "site is required", "try /api/imagery?site=IN-MUN-PORT")
    end = _date(params, "to", datetime.now(timezone.utc).date())
    start = _date(params, "from", end - timedelta(days=60))
    scenes = store.list_scenes(aoi_id, start, end)
    return {"site": aoi_id, "scenes": [
        {"id": s.id, "constellation": s.constellation.value,
         "sensor": s.sensor.value, "acquired_on": s.acquired_on.isoformat(),
         "gsd_m": s.gsd_m, "cloud_pct": s.cloud_pct,
         "sun_elevation_deg": s.sun_elevation_deg, "orbit": s.orbit,
         "usable": s.usable, "unusable_reason": s.unusable_reason}
        for s in scenes]}


@route("GET", "/api/changes")
def api_changes(store: Store, params: dict, **_) -> dict:
    end = _date(params, "to", datetime.now(timezone.utc).date())
    start = _date(params, "from", end - timedelta(days=30))
    rows = store.list_changes(_one(params, "site") or None, start=start, end=end)
    return {"changes": [
        {"id": e.id, "aoi_id": e.aoi_id, "type": e.change_type.value,
         "detected_on": e.detected_at.date().isoformat(),
         "area_m2": e.area_m2, "confidence": e.confidence,
         "severity": e.severity.value, "explanation": e.explanation,
         "geometry": [list(p) for p in e.geometry],
         "before_scene_id": e.before_scene_id, "after_scene_id": e.after_scene_id,
         "evidence_id": e.evidence_id, "review_status": e.review_status.value}
        for e in rows]}


@route("GET", "/api/objects")
def api_objects(store: Store, params: dict, **_) -> dict:
    aoi_id = _one(params, "site")
    if not aoi_id:
        raise ApiError(400, "site is required")
    rows = store.list_detections(aoi_id, _one(params, "scene") or None)
    return {"site": aoi_id, "objects": [
        {"id": d.id, "class": d.object_class.value, "confidence": d.confidence,
         "lon": d.lon, "lat": d.lat, "extent_m": d.extent_m,
         "scene_id": d.scene_id, "observed_at": d.observed_at.isoformat(),
         "model_version": d.model_version} for d in rows]}


@route("GET", "/api/alerts")
def api_alerts(store: Store, params: dict, **_) -> dict:
    max_priority = int(_one(params, "max_priority", "4"))
    status = _one(params, "status")
    rows = store.list_alerts(
        _one(params, "site") or None, max_priority=max_priority,
        status=ReviewStatus(status) if status else None)
    return {"alerts": rows, "queue": {
        "total": len(rows),
        **{f"priority_{p}": sum(1 for r in rows if r["priority"] == p)
           for p in (1, 2, 3, 4)}}}


@route("GET", "/api/timeline")
def api_timeline(store: Store, params: dict, **_) -> dict:
    """Scenes, changes and anomaly scores on one axis.

    Including the scenes that were rejected: a timeline that shows only
    findings cannot distinguish a quiet month from a cloudy one.
    """
    aoi_id = _one(params, "site")
    if not aoi_id:
        raise ApiError(400, "site is required")
    end = _date(params, "to", datetime.now(timezone.utc).date())
    start = _date(params, "from", end - timedelta(days=180))
    scenes = store.list_scenes(aoi_id, start, end)
    changes = store.list_changes(aoi_id, start=start, end=end, limit=2000)
    anomalies = [a for a in store.list_anomalies(aoi_id, limit=2000)
                 if start <= a.observed_at.date() <= end]
    return {
        "site": aoi_id,
        "period": {"from": start.isoformat(), "to": end.isoformat()},
        "acquisitions": [
            {"date": s.acquired_on.isoformat(), "constellation": s.constellation.value,
             "sensor": s.sensor.value, "usable": s.usable,
             "cloud_pct": s.cloud_pct, "reason": s.unusable_reason}
            for s in scenes],
        "changes": [
            {"date": e.detected_at.date().isoformat(), "type": e.change_type.value,
             "area_m2": e.area_m2, "severity": e.severity.value, "id": e.id}
            for e in changes],
        "anomaly_scores": [
            {"date": a.observed_at.date().isoformat(), "score": a.score,
             "confidence": a.confidence} for a in anomalies],
    }


@route("GET", "/api/evidence/{evidence_id}")
def api_evidence(store: Store, evidence_id: str, **_) -> dict:
    doc = store.get_evidence(evidence_id)
    if doc is None:
        raise ApiError(404, f"no evidence bundle {evidence_id}")
    return doc


@route("GET", "/api/watchlists")
def api_watchlists(store: Store, **_) -> dict:
    return {"watchlists": [
        {"id": w.id, "name": w.name, "priority": w.priority.value,
         "aoi_ids": w.aoi_ids} for w in store.list_watchlists()]}


@route("GET", "/api/rules")
def api_rules(store: Store, **_) -> dict:
    return {"rules": [r.to_dict() for r in store.list_rules(enabled_only=False)]}


@route("GET", "/api/reports")
def api_reports(store: Store, **_) -> dict:
    return {"reports": store.list_reports()}


@route("GET", "/api/audit")
def api_audit(store: Store, params: dict, **_) -> dict:
    intact, bad = store.verify_audit_chain()
    return {"chain_intact": intact, "first_bad_entry": bad,
            "entries": store.audit_entries(int(_one(params, "limit", "100")))}


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

@route("POST", "/api/analysis")
def api_analysis(store: Store, body: dict, **_) -> dict:
    """PRD section 40's `POST /analyze`: an AOI and a date range in, findings out.

    Read-only against stored results rather than triggering a fresh run.
    Tasking imagery costs money per square kilometre and re-running detection
    costs compute, so neither happens because an unauthenticated caller sent a
    POST. `terrashield monitor` is the entry point that does the work.
    """
    aoi_id = body.get("site") or body.get("aoi_id")
    if not aoi_id:
        raise ApiError(400, "site is required in the request body")
    aoi = store.get_aoi(aoi_id)
    if aoi is None:
        raise ApiError(404, f"no monitored area {aoi_id}")
    end = _body_date(body, "to", datetime.now(timezone.utc).date())
    start = _body_date(body, "from", end - timedelta(days=30))
    if start > end:
        raise ApiError(400, "'from' is after 'to'",
                       "an inverted range matches nothing, which is "
                       "indistinguishable from a period with no findings")
    changes = store.list_changes(aoi_id, start=start, end=end, limit=1000)
    anomalies = [a for a in store.list_anomalies(aoi_id, limit=1000)
                 if start <= a.observed_at.date() <= end]
    return {
        "site": aoi_id,
        "period": {"from": start.isoformat(), "to": end.isoformat()},
        "objects": len(store.list_detections(aoi_id)),
        "changes": [
            {"id": e.id, "type": e.change_type.value, "area_m2": e.area_m2,
             "confidence": e.confidence, "severity": e.severity.value,
             "detected_on": e.detected_at.date().isoformat(),
             "explanation": e.explanation, "evidence_id": e.evidence_id}
            for e in changes],
        "anomalies": [
            {"id": a.id, "date": a.observed_at.date().isoformat(),
             "score": a.score, "confidence": a.confidence,
             "reasons": a.reasons} for a in anomalies],
        "caveat": ("findings describe observed change and statistical deviation "
                   "from this site's own history; they carry no assessment of "
                   "cause or intent"),
    }


@route("POST", "/api/copilot")
def api_copilot(store: Store, body: dict, **_) -> dict:
    question = (body.get("question") or "").strip()
    if not question:
        raise ApiError(400, "question is required")
    return copilot.ask(store, question).to_dict()


@route("POST", "/api/reviews/{finding_id}")
def api_review(store: Store, finding_id: str, body: dict, **_) -> dict:
    status = body.get("status", "")
    try:
        review = ReviewStatus(status)
    except ValueError:
        raise ApiError(400, "status must be one of: "
                            + ", ".join(s.value for s in ReviewStatus))
    note = body.get("note", "")
    if finding_id.startswith("alert-"):
        return {"alert": store.review_alert(finding_id, review, note)}
    event = store.review_change(finding_id, review, note)
    return {"change": {"id": event.id, "review_status": event.review_status.value,
                       "reviewed_by": event.reviewed_by, "note": event.review_note}}


@route("POST", "/api/watchlists")
def api_create_watchlist(store: Store, body: dict, **_) -> dict:
    from .domain import Severity, Watchlist
    wl = Watchlist(id=body.get("id") or f"wl-{len(store.list_watchlists()) + 1}",
                   org_id=store.org_id, name=body.get("name", "Watchlist"),
                   aoi_ids=list(body.get("aoi_ids", [])),
                   priority=Severity(body.get("priority", "medium")))
    store.put_watchlist(wl)
    return {"watchlist": {"id": wl.id, "name": wl.name, "aoi_ids": wl.aoi_ids}}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def dispatch(store: Store, method: str, path: str, params: dict,
             body: dict | None = None) -> tuple[int, dict]:
    for verb, regex, fn in _ROUTES:
        if verb != method:
            continue
        m = regex.match(path)
        if not m:
            continue
        try:
            return 200, fn(store=store, params=params, body=body or {},
                           **m.groupdict())
        except ApiError as e:
            return e.status, {"error": e.message, "hint": e.hint}
        except AccessDenied as e:
            return 403, {"error": str(e)}
        except TenantViolation as e:
            return 403, {"error": str(e)}
        except StoreError as e:
            return 409, {"error": str(e)}
        except Exception as e:                       # noqa: BLE001
            return 500, {"error": f"{type(e).__name__}: {e}",
                         "trace": traceback.format_exc(limit=4)}
    return 404, {"error": f"no route for {method} {path}",
                 "routes": sorted({f"{v} {r.pattern}" for v, r, _ in _ROUTES})}


# ---------------------------------------------------------------------------
# Sign-up and sign-in
# ---------------------------------------------------------------------------

def _need(body: dict, key: str) -> str:
    value = str(body.get(key, "")).strip()
    if not value:
        raise ApiError(400, f"{key} is required")
    return value


@public_route("POST", "/api/auth/signup")
def api_signup(accounts: Accounts, body: dict, **_) -> dict:
    """Create an organisation and its first administrator.

    Self-service sign-up is on because this is how a pilot starts. A deployment
    that federates to a customer directory should remove this route rather than
    guard it -- an unused registration endpoint is an unused attack surface.
    """
    try:
        account = accounts.register_organisation(
            org_name=_need(body, "organisation"),
            email=_need(body, "email"),
            password=_need(body, "password"),
            name=str(body.get("name", "")))
    except AuthError as e:
        raise ApiError(400, str(e)) from e
    session = accounts.open_session(account)
    return session.to_dict()


@public_route("POST", "/api/auth/login")
def api_login(accounts: Accounts, body: dict, **_) -> dict:
    try:
        session = accounts.sign_in(_need(body, "email"), _need(body, "password"))
    except AuthError as e:
        #: 401 for every failure, with the message accounts.py already made
        #: uniform. The status code must not distinguish the cases either.
        raise ApiError(401, str(e)) from e
    return session.to_dict()


@public_route("POST", "/api/auth/logout")
def api_logout(accounts: Accounts, token: str, **_) -> dict:
    return {"revoked": accounts.revoke_session(token)}


# ---------------------------------------------------------------------------
# Areas and fields
# ---------------------------------------------------------------------------

def _ring(body: dict, key: str = "boundary") -> list:
    """A polygon from either a GeoJSON document or a bare ring of points.

    Accepting both is not indulgence: a drawing tool hands you GeoJSON and a
    script hands you a list of pairs, and rejecting either means somebody
    writes a conversion by hand and gets the winding order wrong.
    """
    raw = body.get(key)
    if raw is None:
        raise ApiError(400, f"{key} is required",
                       "a GeoJSON Polygon/Feature, or a list of [lon, lat] pairs")
    if isinstance(raw, dict):
        try:
            rings = rings_from_geojson(raw)
        except (KeyError, TypeError, ValueError) as e:
            raise ApiError(400, f"{key} is not valid GeoJSON: {e}") from e
        if not rings:
            raise ApiError(400, f"{key} contains no polygon")
        return rings[0]
    if isinstance(raw, list):
        try:
            return [(float(x), float(y)) for x, y in raw]
        except (TypeError, ValueError) as e:
            raise ApiError(400, f"{key} must be a list of [lon, lat] pairs") from e
    raise ApiError(400, f"{key} must be GeoJSON or a list of [lon, lat] pairs")


@route("POST", "/api/sites")
def api_create_site(store: Store, body: dict, **_) -> dict:
    """Add an area to monitor."""
    kind_raw = str(body.get("kind", AoiKind.GENERIC.value))
    try:
        kind = AoiKind(kind_raw)
    except ValueError as e:
        raise ApiError(400, f"{kind_raw!r} is not a known area kind",
                       "one of: " + ", ".join(k.value for k in AoiKind)) from e
    aoi = Aoi(id=str(body.get("id") or f"aoi-{uuid.uuid4().hex[:12]}"),
              org_id=store.org_id, name=_need(body, "name"),
              boundary=_ring(body), kind=kind,
              country=str(body.get("country", "")),
              description=str(body.get("description", "")))
    try:
        store.put_aoi(aoi)
    except StoreError as e:
        #: The geometry problems this raises are the caller's to fix, and each
        #: one names what is wrong with it. A 500 here would hide that.
        raise ApiError(400, str(e)) from e
    return {"site": {"id": aoi.id, "name": aoi.name, "kind": aoi.kind.value,
                     "area_km2": round(aoi.area_km2, 4),
                     "centroid": list(aoi.centroid), "bbox": list(aoi.bbox),
                     "fingerprint": aoi.fingerprint}}


def _field_dict(f: Field) -> dict:
    return {"id": f.id, "aoi_id": f.aoi_id, "name": f.name, "use": f.use,
            "notes": f.notes, "hectares": round(f.area_hectares, 3),
            "area_km2": round(f.area_km2, 5), "centroid": list(f.centroid),
            "bbox": list(f.bbox), "boundary": [list(p) for p in f.boundary],
            "fingerprint": f.fingerprint, "active": f.active}


@route("GET", "/api/sites/{aoi_id}/fields")
def api_fields(store: Store, aoi_id: str, **_) -> dict:
    if store.get_aoi(aoi_id) is None:
        raise ApiError(404, f"no monitored area {aoi_id}")
    fields = store.list_fields(aoi_id)
    return {"aoi_id": aoi_id, "count": len(fields),
            "hectares": round(sum(f.area_hectares for f in fields), 2),
            "fields": [_field_dict(f) for f in fields]}


@route("POST", "/api/sites/{aoi_id}/fields")
def api_create_field(store: Store, aoi_id: str, body: dict, **_) -> dict:
    fld = Field(id=str(body.get("id") or f"fld-{uuid.uuid4().hex[:12]}"),
                org_id=store.org_id, aoi_id=aoi_id,
                name=_need(body, "name"), boundary=_ring(body),
                use=str(body.get("use", "")), notes=str(body.get("notes", "")))
    try:
        store.put_field(fld)
    except StoreError as e:
        raise ApiError(400, str(e)) from e
    return {"field": _field_dict(fld)}


@route("POST", "/api/fields/{field_id}/retire")
def api_retire_field(store: Store, field_id: str, **_) -> dict:
    """Retire a field. Idempotent, and says which of the two happened.

    A field that was already retired is not an error -- repeating the call is
    how every retry works -- but it is also not the same event, and collapsing
    the two would tell a caller it had just changed something when it had not.
    """
    changed = store.deactivate_field(field_id)
    if not changed and store.get_field(field_id) is None:
        raise ApiError(404, f"no field {field_id}")
    return {"retired": field_id, "already_retired": not changed}


@route("GET", "/api/fields/at")
def api_field_at(store: Store, params: dict, **_) -> dict:
    """Which field a coordinate falls in. What turns a centroid into 'Block D'."""
    try:
        lon = float(_one(params, "lon", ""))
        lat = float(_one(params, "lat", ""))
    except ValueError as e:
        raise ApiError(400, "lon and lat are required and must be numbers") from e
    fld = store.field_at(lon, lat, _one(params, "aoi_id", "") or None)
    return {"field": _field_dict(fld) if fld else None}


def dispatch_public(accounts: Accounts, method: str, path: str,
                    params: dict, body: dict, token: str) -> tuple[int, dict]:
    """Route a request that has not been authenticated yet.

    Returns (0, {}) when nothing matches, which tells the caller to fall
    through to the authenticated table rather than 404 -- an unknown path must
    still demand a token, or the API would report which routes exist to anyone
    who asked.
    """
    for route_method, regex, fn in _PUBLIC_ROUTES:
        if route_method != method:
            continue
        match = regex.match(path)
        if not match:
            continue
        try:
            return 200, fn(accounts=accounts, body=body, params=params,
                           token=token, **match.groupdict())
        except ApiError as e:
            payload = {"error": e.message}
            if e.hint:
                payload["hint"] = e.hint
            return e.status, payload
        except Exception:                                    # pragma: no cover
            traceback.print_exc()
            return 500, {"error": "internal error"}
    return 0, {}


def make_handler(store_factory: Callable[[str], Store],
                 accounts_factory: Callable[[], Accounts] | None = None):
    class TerraShieldHandler(BaseHTTPRequestHandler):
        server_version = "TerraShield/0.1"

        def _send(self, status: int, payload: dict) -> None:
            blob = json.dumps(payload, indent=2, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(blob)

        def _token(self) -> str:
            auth = self.headers.get("Authorization", "")
            return auth[7:].strip() if auth.lower().startswith("bearer ") else ""

        def _handle(self, method: str) -> None:
            url = urlparse(self.path)
            params = parse_qs(url.query)
            body = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    try:
                        body = json.loads(self.rfile.read(length) or b"{}")
                    except json.JSONDecodeError:
                        self._send(400, {"error": "request body is not valid JSON"})
                        return
            token = self._token()

            if accounts_factory is not None:
                accounts = accounts_factory()
                try:
                    status, payload = dispatch_public(
                        accounts, method, url.path, params, body, token)
                finally:
                    accounts.conn.close()
                if status:
                    self._send(status, payload)
                    return

            try:
                store = store_factory(token)
            except PermissionError as e:
                self._send(401, {"error": str(e)})
                return
            try:
                status, payload = dispatch(store, method, url.path, params, body)
            finally:
                store.close()
            self._send(status, payload)

        def do_GET(self) -> None:       # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:      # noqa: N802
            self._handle("POST")

        def log_message(self, fmt: str, *args) -> None:
            pass                        # the audit log is the record that matters

    return TerraShieldHandler


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8787,
          org_id: str = "", token_map: dict[str, tuple[str, str, Role]] | None = None,
          allow_signup: bool = True) -> None:
    """Run the API.

    Identity comes from a session token issued by /api/auth/login, resolved
    against the accounts table on every request. That resolution is the seam an
    external identity provider replaces: everything above it wants
    (org, actor, role) and does not care how they were established.

    `token_map` still works and still takes precedence, because a static token
    is the right thing for a service account or a smoke test. What has not
    changed is that with no way to authenticate at all -- no accounts, no map,
    no environment token -- the server refuses to start. A monitoring platform
    that defaults to open is a monitoring platform that ships open.
    """
    tokens = dict(token_map or {})
    env_token = os.environ.get("TERRASHIELD_TOKEN")
    if env_token and org_id:
        tokens.setdefault(env_token, (org_id, "api-token", Role.ANALYST))

    def _connect() -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        migrate(conn)
        return conn

    #: Sign-up is only a way in if the deployment has accounts at all. A
    #: database with no users and no static token is refused below rather than
    #: served open.
    probe = _connect()
    try:
        users = probe.execute("SELECT COUNT(*) FROM users "
                              "WHERE password_hash != ''").fetchone()[0]
    finally:
        probe.close()
    if not tokens and not users and not allow_signup:
        raise SystemExit(
            "refusing to serve without authentication: create a user, pass "
            "token_map, set TERRASHIELD_TOKEN with an organisation id, or "
            "start with allow_signup=True to enable self-service registration")

    def accounts_factory() -> Accounts:
        return Accounts(_connect())

    def factory(token: str) -> Store:
        entry = tokens.get(token)
        if entry is not None:
            org, actor, role = entry
            return Store(db_path, org_id=org, actor=actor, role=role)

        conn = _connect()
        try:
            account = Accounts(conn).resolve_session(token)
        finally:
            conn.close()
        if account is None:
            raise PermissionError("a valid bearer token is required")
        return Store(db_path, org_id=account.org_id, actor=account.email,
                     role=account.role)

    server = ThreadingHTTPServer(
        (host, port),
        make_handler(factory, accounts_factory if allow_signup else None))
    print(f"TerraShield API on http://{host}:{port}  (database {db_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
