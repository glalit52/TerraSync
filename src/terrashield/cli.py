"""Command line: the monitoring lifecycle, in the order it happens.

    terrashield init                                  create the database
    terrashield enroll mundra                         load a demo area
    terrashield enroll --geojson sector.json --name "Sector 12"
    terrashield monitor IN-MUN-PORT --from 2026-04-01 --to 2026-05-31
    terrashield coverage IN-MUN-PORT --days 180       what the sensors delivered
    terrashield queue                                 what needs an analyst
    terrashield explain chg-abc123                    where a finding came from
    terrashield review chg-abc123 --confirm --note "new warehouse, confirmed"
    terrashield ask "what changed at Mundra in the last 30 days"
    terrashield report weekly --to 2026-05-31
    terrashield evidence IN-MUN-PORT --out packs/mun  an exportable pack
    terrashield evaluate                              precision, recall, calibration
    terrashield verify                                is the audit chain intact
    terrashield serve                                 HTTP API

Every command that changes a record or reads imagery takes `--actor`, because
every one of them lands in the audit chain under a name. That is not ceremony:
in an intelligence system, who looked at which site is the question an insider
review actually asks.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import alerts as alert_engine
from . import copilot, detect, evaluate as evaluate_mod, evidence, pipeline, reports, sites
from .catalog import SyntheticProvider, coverage
from .domain import Organization, ReviewStatus, Role, User
from .geo import area_km2, rings_from_geojson, validate_ring
from .store import Store
from .store.repo import StoreError

DEFAULT_DB = "terrashield.db"


def _store(args, role: Role = Role.ADMIN) -> Store:
    return Store(args.db, org_id=args.org, actor=args.actor or "cli", role=role)


def _provider() -> SyntheticProvider:
    """The demo imagery source.

    A real deployment swaps this for a `Provider` backed by Sentinel Hub,
    Planet or Maxar. Nothing above `catalog.Provider` changes when it does.
    """
    p = SyntheticProvider()
    for s in sites.load_all():
        p.register(s.truth, s.climate)
    return p


def _require_actor(args) -> str:
    if not args.actor:
        raise SystemExit(
            "--actor is required: every change and every imagery read is "
            "attributed in the audit chain")
    return args.actor


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init(args) -> int:
    _require_actor(args)
    store = _store(args)
    store.put_org(Organization(id=args.org, name=args.org_name,
                               country=args.country))
    store.put_user(User(id="u-owner", org_id=args.org, name=args.actor,
                        email=args.actor, role=Role.ADMIN))
    for rule in alert_engine.default_rules(args.org):
        store.put_rule(rule)
    print(f"initialised {args.db} at schema version {store.version}")
    print(f"organisation {args.org}  owner {args.actor}")
    print(f"{len(store.list_rules())} default alert rules installed")
    return 0


def cmd_enroll(args) -> int:
    _require_actor(args)
    store = _store(args)
    if args.geojson:
        doc = json.loads(Path(args.geojson).read_text())
        rings = rings_from_geojson(doc)
        if not rings:
            raise SystemExit(f"{args.geojson} contains no polygon geometry")
        from .domain import Aoi, AoiKind
        for i, ring in enumerate(rings, 1):
            problems = validate_ring(ring)
            if problems:
                raise SystemExit(f"ring {i}: " + "; ".join(problems))
            aoi = Aoi(id=args.id or f"AOI-{i:03d}", org_id=args.org,
                      name=args.name or f"Area {i}", boundary=ring,
                      kind=AoiKind(args.kind), country=args.country)
            store.put_aoi(aoi)
            print(f"enrolled {aoi.id}  {aoi.name}  {aoi.area_km2:,.1f} km2")
        return 0

    if not args.site:
        raise SystemExit("give a demo site name or --geojson FILE; demo sites: "
                         + ", ".join(sorted(sites.ALIASES)))
    demo = sites.load(args.site)
    store.put_aoi(demo.aoi)
    print(f"enrolled {demo.aoi.id}  {demo.aoi.name}  "
          f"{demo.aoi.area_km2:,.1f} km2  ({demo.headline})")
    return 0


def cmd_monitor(args) -> int:
    _require_actor(args)
    store = _store(args)
    provider = _provider()
    end = date.fromisoformat(args.to) if args.to else datetime.now(timezone.utc).date()
    start = date.fromisoformat(args.since) if args.since else end - timedelta(days=30)
    targets = ([store.get_aoi(args.aoi_id)] if args.aoi_id
               else store.list_aois())
    if any(t is None for t in targets):
        raise SystemExit(f"no monitored area {args.aoi_id}")

    total_alerts = 0
    for aoi in targets:
        results = pipeline.run_range(store, provider, aoi, start, end)
        worked = [r for r in results if r.scene_used]
        raised = sum(r.alerts for r in results)
        total_alerts += raised
        print(f"{aoi.id:<16} {len(worked):>3} usable day(s) of "
              f"{len(results):>3}   {sum(r.detections for r in worked):>4} objects "
              f" {sum(r.change_events for r in worked):>3} changes "
              f" {raised:>2} alerts")
        for r in results:
            if r.alerts:
                for a in r.raised:
                    print(f"    {r.when}  P{a.alert.priority} "
                          f"{a.alert.severity.value:<8} {a.alert.title}")
    print(f"\n{total_alerts} alert(s) raised. `terrashield queue` to work them.")
    return 0


def cmd_coverage(args) -> int:
    store = _store(args)
    provider = _provider()
    end = date.fromisoformat(args.to) if args.to else datetime.now(timezone.utc).date()
    start = end - timedelta(days=args.days)
    for aoi in ([store.get_aoi(args.aoi_id)] if args.aoi_id else store.list_aois()):
        if aoi is None:
            raise SystemExit(f"no monitored area {args.aoi_id}")
        scenes = provider.search(aoi, start, end)
        cov = coverage(aoi.id, scenes, start, end)
        print(f"\n{aoi.name}  ({aoi.id})")
        print(f"  {cov.acquired} acquisitions, {cov.usable} usable "
              f"({cov.usable_fraction:.0%}) over {args.days} days")
        print(f"  longest gap without a usable look: {cov.longest_gap_days} days")
        print(f"  last usable: {cov.last_usable or 'never'}")
        for c, n in sorted(cov.by_constellation.items(), key=lambda kv: -kv[1]):
            print(f"    {c:<16} {n}")
        for reason, n in sorted(cov.rejected_reasons.items()):
            print(f"    rejected for {reason}: {n}")
    return 0


def cmd_queue(args) -> int:
    store = _store(args)
    rows = store.list_alerts(args.aoi_id or None, max_priority=args.max_priority,
                             limit=args.limit)
    if not rows:
        print("nothing in the queue")
        return 0
    print(f"{len(rows)} alert(s)\n")
    for r in rows:
        payload = r.get("payload", {})
        risk = payload.get("risk") or {}
        print(f"P{r['priority']} {r['severity']:<8} {r['aoi_id']:<16} {r['title']}")
        if r["summary"]:
            print(f"      {r['summary'][:110]}")
        if risk:
            print(f"      risk {risk.get('composite', 0):.0f}  "
                  f"confidence {risk.get('confidence', 0):.0%}  "
                  f"novelty {risk.get('novelty', 0):.0%}  "
                  f"persistence {risk.get('persistence', 0):.0%}")
        if payload.get("held_for_confirmation"):
            print(f"      HELD: {payload['held_for_confirmation']}")
        if r["evidence_id"]:
            print(f"      evidence {r['evidence_id']}")
        print()
    return 0


def cmd_explain(args) -> int:
    _require_actor(args)
    store = _store(args)
    doc = store.evidence_for(args.finding_id) or store.get_evidence(args.finding_id)
    if doc is None:
        raise SystemExit(f"no evidence recorded for {args.finding_id}")
    if args.json:
        print(json.dumps(doc, indent=2))
        return 0
    bundle = evidence.EvidenceBundle(
        id=doc["id"], aoi_id=doc["aoi_id"], finding_id=doc["finding_id"],
        finding_kind=doc["finding_kind"],
        created_at=datetime.fromisoformat(doc["created_at"]),
        artefacts=[evidence.Artefact(**a) for a in doc["artefacts"]],
        measurements=doc["measurements"], gaps=doc["gaps"],
        narrative=doc["narrative"])
    print(bundle.render())
    return 0


def cmd_review(args) -> int:
    _require_actor(args)
    status = (ReviewStatus.CONFIRMED if args.confirm else
              ReviewStatus.REJECTED if args.reject else
              ReviewStatus.ESCALATED if args.escalate else None)
    if status is None:
        raise SystemExit("choose one of --confirm, --reject or --escalate")
    role = Role.SUPERVISOR if args.escalate else Role.ANALYST
    store = _store(args, role=role)
    if args.finding_id.startswith("alert-"):
        row = store.review_alert(args.finding_id, status, args.note)
        print(f"{row['id']}  {row['review_status']}  by {row['reviewed_by']}")
    else:
        event = store.review_change(args.finding_id, status, args.note)
        print(f"{event.id}  {event.review_status.value}  by {event.reviewed_by}")
    return 0


def cmd_ask(args) -> int:
    _require_actor(args)
    store = _store(args)
    #: "the last 30 days" is relative to a date, and the useful date is not
    #: always today: an estate whose sites were monitored over different
    #: windows is queried from inside those windows, and so is any question
    #: asked about a period that has already closed.
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    answer = copilot.ask(store, args.question, today=as_of)
    if args.json:
        print(json.dumps(answer.to_dict(), indent=2))
        return 0
    if answer.unsupported:
        print(f"Not answered: {answer.unsupported}")
        return 0
    print(answer.text)
    if answer.citations:
        print("\nBased on:")
        for c in answer.citations[:10]:
            print(f"  {c.kind:<10} {c.ref}" + (f"  {c.detail}" if c.detail else ""))
    return 0


def cmd_report(args) -> int:
    _require_actor(args)
    store = _store(args, role=Role.SUPERVISOR)
    end = date.fromisoformat(args.to) if args.to else datetime.now(timezone.utc).date()
    if args.kind == "daily":
        report = reports.daily(store, args.org, end)
    elif args.kind == "weekly":
        report = reports.weekly(store, args.org, end)
    else:
        aoi = store.get_aoi(args.aoi_id or "")
        if aoi is None:
            raise SystemExit("a site report needs --site AOI_ID")
        report = reports.site(store, aoi, end - timedelta(days=args.days), end)
    store.put_report(report)
    if args.out:
        Path(args.out).write_text(report.body_markdown)
        print(f"wrote {args.out}")
    else:
        print(report.body_markdown)
    return 0


def cmd_evidence(args) -> int:
    _require_actor(args)
    store = _store(args, role=Role.SUPERVISOR)
    aoi = store.get_aoi(args.aoi_id)
    if aoi is None:
        raise SystemExit(f"no monitored area {args.aoi_id}")
    end = date.fromisoformat(args.to) if args.to else datetime.now(timezone.utc).date()
    start = end - timedelta(days=args.days)
    docs = store.list_evidence(aoi.id, start, end)
    bundles = [evidence.EvidenceBundle(
        id=d["id"], aoi_id=d["aoi_id"], finding_id=d["finding_id"],
        finding_kind=d["finding_kind"],
        created_at=datetime.fromisoformat(d["created_at"]),
        finding_at=(datetime.fromisoformat(d["finding_at"])
                    if d.get("finding_at") else None),
        artefacts=[evidence.Artefact(**a) for a in d["artefacts"]],
        measurements=d["measurements"], gaps=d["gaps"],
        narrative=d["narrative"]) for d in docs]
    doc = evidence.pack(bundles, aoi, (start, end))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "pack.json").write_text(json.dumps(doc, indent=2))
    (out / "SUMMARY.md").write_text(_pack_summary(doc, bundles))
    ok, problems = evidence.verify(doc)
    print(f"wrote {len(bundles)} bundle(s) to {out}")
    print(f"manifest {doc['manifest_sha256'][:32]}  verifies: {ok}")
    for p in problems:
        print(f"  {p}")
    return 0


def _pack_summary(doc: dict, bundles: list) -> str:
    lines = [
        f"# Evidence pack — {doc['aoi']['name']}",
        "",
        f"Area `{doc['aoi']['id']}` · {doc['aoi']['area_km2']} km2 · boundary "
        f"fingerprint `{doc['aoi']['fingerprint']}`",
        f"Period {doc['period']['start']} to {doc['period']['end']}",
        f"Manifest SHA-256 `{doc['manifest_sha256']}`",
        "",
        f"{doc['bundle_count']} finding(s). Each bundle below lists the scenes "
        "it was derived from, the model version that produced it, and what "
        "could not be established from the imagery available.",
        "",
    ]
    for b in bundles:
        lines += ["```", b.render(), "```", ""]
    return "\n".join(lines)


def cmd_evaluate(args) -> int:
    """Measure the engines against known truth. PRD section 54's metrics."""
    provider = _provider()
    total = evaluate_mod.Score()
    print(f"{'site':<16} {'P':>5} {'R':>5} {'F1':>5} {'FP/km2':>7}  scenes")
    for demo in sites.load_all():
        acc = evaluate_mod.Score()
        scenes = [s for s in provider.search(demo.aoi,
                                             date.fromisoformat(args.since),
                                             date.fromisoformat(args.to))
                  if s.usable and s.sensor.value == "optical"][:args.scenes]
        for scene in scenes:
            r = provider.fetch(demo.aoi, scene)
            masks = provider.masks(demo.aoi, scene, r)
            res = detect.detect(demo.aoi, scene, r, obscured=masks.cloud,
                                water=masks.water)
            acc.add(evaluate_mod.score_scene(
                demo.truth, demo.aoi.boundary, scene.acquired_on, res.detections,
                min_extent_m=2.5 * r.gsd_m, min_width_m=1.5 * r.gsd_m,
                area_km2=demo.aoi.area_km2))
        total.add(acc)
        print(f"{demo.id:<16} {acc.precision:>5.2f} {acc.recall:>5.2f} "
              f"{acc.f1:>5.2f} {acc.false_positives_per_km2:>7.3f}  {len(scenes)}")
    print(f"\n{'TOTAL':<16} {total.precision:>5.2f} {total.recall:>5.2f} "
          f"{total.f1:>5.2f} {total.false_positives_per_km2:>7.3f}")
    print("\nconfidence calibration — does 0.8 mean right eight times in ten?")
    for b in detect.calibration_report(total.confidence_pairs):
        if b.n:
            print(f"  [{b.low:.2f},{b.high:.2f})  n={b.n:<4} observed "
                  f"{b.observed:.2f}  expected {b.expected:.2f}  "
                  f"gap {b.gap:+.2f}")
    print("\nMeasured against the synthetic estate in sites.py. Re-run against "
          "labelled customer scenes before quoting these numbers to a customer.")
    return 0


def cmd_verify(args) -> int:
    store = _store(args)
    intact, bad = store.verify_audit_chain()
    print(f"database        {store.path}")
    print(f"schema version  {store.version}")
    for k, v in store.counts().items():
        print(f"  {k:<16} {v}")
    print(f"audit chain     {'intact' if intact else f'BROKEN at entry {bad}'}")
    return 0 if intact else 1


def cmd_serve(args) -> int:
    from .api import serve
    tokens = {args.token: (args.org, args.actor or "api", Role.ANALYST)} \
        if args.token else None
    serve(args.db, host=args.host, port=args.port, org_id=args.org,
          token_map=tokens)
    return 0


def cmd_demo(args) -> int:
    """Build the whole demo estate from nothing. One command, for a first look.

    The default window is six weeks, not six months, because a demonstration
    that takes two hours is not a demonstration. Six weeks covers one site's
    scripted event (Bhadla commissions an array block on 2026-03-19) and gives
    every other site enough history for a baseline, which is what a first look
    needs to show.

    For the full estate -- each site monitored across its own events, which is
    what the committed console is built from -- use
    scripts/build_demo.sh.
    """
    _require_actor(args)
    args.org_name = "Sensegrass Demo"
    args.country = "IN"
    cmd_init(args)
    store = _store(args)
    provider = _provider()
    end = date.fromisoformat(args.to)
    start = date.fromisoformat(args.since)
    for demo in sites.load_all():
        store.put_aoi(demo.aoi)
        print(f"\n{demo.aoi.name}")
        results = pipeline.run_range(store, provider, demo.aoi, start, end)
        worked = [r for r in results if r.scene_used]
        print(f"  {len(worked)} usable day(s), "
              f"{sum(r.change_events for r in worked)} changes, "
              f"{sum(r.alerts for r in worked)} alerts")
    print("\n" + "-" * 60)
    cmd_queue(argparse.Namespace(**{**vars(args), "aoi_id": None,
                                    "max_priority": 2, "limit": 10}))
    print("This is a six-week window. For the full estate, each site monitored "
          "across its own\nscripted events, run "
          "scripts/build_demo.sh.")
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="terrashield", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--org", default=sites.DEMO_ORG)
    p.add_argument("--actor", default="", help="who is acting; audited")
    sub = p.add_subparsers(dest="command", required=True)

    q = sub.add_parser("init", help="create the database and default rules")
    q.add_argument("--org-name", default="Sensegrass Demo")
    q.add_argument("--country", default="IN")
    q.set_defaults(fn=cmd_init)

    q = sub.add_parser("enroll", help="add a monitored area")
    q.add_argument("site", nargs="?", default="")
    q.add_argument("--geojson"), q.add_argument("--name", default="")
    q.add_argument("--id", default=""), q.add_argument("--kind", default="generic")
    q.add_argument("--country", default="IN")
    q.set_defaults(fn=cmd_enroll)

    q = sub.add_parser("monitor", help="run the pipeline over a date range")
    q.add_argument("aoi_id", nargs="?", default="")
    q.add_argument("--from", dest="since"), q.add_argument("--to")
    q.set_defaults(fn=cmd_monitor)

    q = sub.add_parser("coverage", help="what the constellation actually delivered")
    q.add_argument("aoi_id", nargs="?", default="")
    q.add_argument("--days", type=int, default=180), q.add_argument("--to")
    q.set_defaults(fn=cmd_coverage)

    q = sub.add_parser("queue", help="alerts waiting for an analyst")
    q.add_argument("aoi_id", nargs="?", default="")
    q.add_argument("--max-priority", type=int, default=4)
    q.add_argument("--limit", type=int, default=50)
    q.set_defaults(fn=cmd_queue)

    q = sub.add_parser("explain", help="the evidence behind a finding")
    q.add_argument("finding_id"), q.add_argument("--json", action="store_true")
    q.set_defaults(fn=cmd_explain)

    q = sub.add_parser("review", help="record an analyst verdict")
    q.add_argument("finding_id")
    q.add_argument("--confirm", action="store_true")
    q.add_argument("--reject", action="store_true")
    q.add_argument("--escalate", action="store_true")
    q.add_argument("--note", default="")
    q.set_defaults(fn=cmd_review)

    q = sub.add_parser("ask", help="ask the copilot, grounded in the record")
    q.add_argument("question"), q.add_argument("--json", action="store_true")
    q.add_argument("--as-of", default="",
                   help="anchor relative periods to this date instead of today")
    q.set_defaults(fn=cmd_ask)

    q = sub.add_parser("report", help="daily, weekly or site report")
    q.add_argument("kind", choices=["daily", "weekly", "site"])
    q.add_argument("--site", dest="aoi_id", default="")
    q.add_argument("--to"), q.add_argument("--days", type=int, default=90)
    q.add_argument("--out", default="")
    q.set_defaults(fn=cmd_report)

    q = sub.add_parser("evidence", help="export a verifiable evidence pack")
    q.add_argument("aoi_id"), q.add_argument("--out", default="pack")
    q.add_argument("--days", type=int, default=180), q.add_argument("--to")
    q.set_defaults(fn=cmd_evidence)

    q = sub.add_parser("evaluate", help="precision, recall and calibration")
    q.add_argument("--from", dest="since", default="2026-02-01")
    q.add_argument("--to", default="2026-03-31")
    q.add_argument("--scenes", type=int, default=3)
    q.set_defaults(fn=cmd_evaluate)

    q = sub.add_parser("verify", help="check the audit chain")
    q.set_defaults(fn=cmd_verify)

    q = sub.add_parser("serve", help="run the HTTP API")
    q.add_argument("--host", default="127.0.0.1")
    q.add_argument("--port", type=int, default=8787)
    q.add_argument("--token", default="")
    q.set_defaults(fn=cmd_serve)

    q = sub.add_parser(
        "demo", help="build the demo estate end to end (six weeks by default)")
    q.add_argument("--from", dest="since", default="2026-03-01")
    q.add_argument("--to", default="2026-04-12")
    q.set_defaults(fn=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except StoreError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
