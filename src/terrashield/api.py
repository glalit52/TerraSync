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
import traceback
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from . import copilot
from .domain import ReviewStatus, Role
from .pipeline import health
from .rbac import AccessDenied, TenantViolation
from .store.repo import Store, StoreError

Handler = Callable[..., Any]
_ROUTES: list[tuple[str, re.Pattern, Handler]] = []


def route(method: str, pattern: str):
    """Register a handler. `{name}` in the pattern becomes a keyword argument."""
    regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

    def wrap(fn: Handler) -> Handler:
        _ROUTES.append((method, regex, fn))
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


def make_handler(store_factory: Callable[[str], Store]):
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
            try:
                store = store_factory(self._token())
            except PermissionError as e:
                self._send(401, {"error": str(e)})
                return
            status, payload = dispatch(store, method, url.path, params, body)
            self._send(status, payload)

        def do_GET(self) -> None:       # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:      # noqa: N802
            self._handle("POST")

        def log_message(self, fmt: str, *args) -> None:
            pass                        # the audit log is the record that matters

    return TerraShieldHandler


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8787,
          org_id: str = "", token_map: dict[str, tuple[str, str, Role]] | None = None
          ) -> None:
    """Run the API.

    `token_map` maps bearer tokens to (org_id, actor, role). In a real
    deployment this is SSO, and the map is the seam where that plugs in. With
    no map, the server refuses to start rather than serving unauthenticated --
    a monitoring platform that defaults to open is a monitoring platform that
    ships open.
    """
    tokens = dict(token_map or {})
    env_token = os.environ.get("TERRASHIELD_TOKEN")
    if env_token and org_id:
        tokens.setdefault(env_token, (org_id, "api-token", Role.ANALYST))
    if not tokens:
        raise SystemExit(
            "refusing to serve without authentication: pass token_map, or set "
            "TERRASHIELD_TOKEN together with an organisation id")

    def factory(token: str) -> Store:
        entry = tokens.get(token)
        if entry is None:
            raise PermissionError("a valid bearer token is required")
        org, actor, role = entry
        return Store(db_path, org_id=org, actor=actor, role=role)

    server = ThreadingHTTPServer((host, port), make_handler(factory))
    print(f"TerraShield API on http://{host}:{port}  (database {db_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
