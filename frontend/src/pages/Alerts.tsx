import clsx from "clsx";
import { Check, ChevronsUp, ExternalLink, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useAlerts, useEvidence, useReview, useSites } from "../api/hooks";
import type { Alert, ReviewStatus } from "../api/types";
import { Card, Caveat, Empty, ErrorState, KeyValue, Loading, PageHeader, PriorityBadge, Q, ReviewBadge, SeverityBadge } from "../components/ui";
import { fmtDateTime, humanise } from "../lib/format";
import { useAuth } from "../state/auth";

function RiskBars({ risk }: { risk: Record<string, number | string> }) {
  const dims = Object.entries(risk).filter(([, v]) => typeof v === "number") as [string, number][];
  if (!dims.length) return null;
  return (
    <div className="space-y-1.5">
      {dims.map(([k, v]) => (
        <div key={k} className="grid grid-cols-[110px_1fr_40px] items-center gap-2 text-xs">
          <span className="text-dim">{humanise(k)}</span>
          <div className="h-1.5 overflow-hidden rounded bg-raised">
            <div className="h-full rounded bg-accent" style={{ width: `${Math.min(100, v <= 1 ? v * 100 : v)}%` }} />
          </div>
          <span className="text-right font-mono">{v <= 1 ? v.toFixed(2) : v.toFixed(0)}</span>
        </div>
      ))}
    </div>
  );
}

function AlertDetail({ alert }: { alert: Alert }) {
  const { can } = useAuth();
  const review = useReview();
  const evidence = useEvidence(alert.evidence_id || undefined);
  const [note, setNote] = useState("");

  useEffect(() => {
    setNote("");
  }, [alert.id]);

  const act = (status: ReviewStatus) => review.mutate({ id: alert.id, status, note });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      if (!can("finding.review")) return;
      if (e.key === "c") act("confirmed");
      if (e.key === "r") act("rejected");
      if (e.key === "e" && can("finding.escalate")) act("escalated");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const p = alert.payload ?? {};
  return (
    <div className="space-y-4">
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <PriorityBadge p={alert.priority} />
          <SeverityBadge severity={alert.severity} />
          <ReviewBadge status={alert.review_status} />
          {p.held_for_confirmation ? <span className="chip border-info/50 text-info">Held: {p.held_for_confirmation}</span> : null}
        </div>
        <h2 className="mt-2 text-lg font-semibold">{alert.title}</h2>
        <p className="mt-1 text-sm text-dim">{alert.summary}</p>
      </div>

      <KeyValue
        rows={[
          ["Site", <Link key="s" className="text-accent hover:underline" to={`/sites/${alert.aoi_id}`}>{alert.aoi_id}</Link>],
          ["Raised", fmtDateTime(alert.created_at)],
          ["Rule", p.rule_name ?? alert.rule_id],
          ["Seen", `${alert.occurrences} time(s) · dedup ${alert.dedup_key}`],
          ["Channels", (p.channels ?? []).join(", ") || "dashboard"],
          ...(alert.reviewed_by ? ([["Reviewed", `${alert.reviewed_by} · ${fmtDateTime(alert.reviewed_at)}`]] as [string, string][]) : []),
          ...(alert.review_note ? ([["Note", alert.review_note]] as [string, string][]) : []),
        ]}
      />

      {p.matched_conditions?.length ? (
        <div>
          <div className="card-t mb-1.5">Why this fired</div>
          <ul className="space-y-1 text-sm">
            {p.matched_conditions.map((c) => (
              <li key={c} className="flex gap-2">
                <Check className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent" /> {c}
              </li>
            ))}
          </ul>
          {p.rule && <p className="mt-2 rounded bg-raised px-2 py-1 font-mono text-[11px] text-dim">{p.rule}</p>}
        </div>
      ) : null}

      {p.risk && typeof p.risk === "object" ? (
        <div>
          <div className="card-t mb-1.5">Triage dimensions</div>
          <RiskBars risk={p.risk} />
        </div>
      ) : null}

      <div>
        <div className="card-t mb-1.5">Evidence</div>
        {evidence.isLoading ? (
          <Loading label="Loading evidence" />
        ) : evidence.error ? (
          <ErrorState error={evidence.error} />
        ) : evidence.data ? (
          <div className="space-y-2 rounded-md border border-line p-3">
            <p className="text-sm">{evidence.data.narrative}</p>
            {evidence.data.gaps?.length ? (
              <div className="text-xs text-dim">
                <span className="font-medium text-med">Not established: </span>
                {evidence.data.gaps.slice(0, 3).join("; ")}
                {evidence.data.gaps.length > 3 ? ` (+${evidence.data.gaps.length - 3} more)` : ""}
              </div>
            ) : null}
            <div className="flex items-center justify-between text-xs text-dim">
              <span>{evidence.data.artefacts.length} artefacts · sha256 {evidence.data.sha256.slice(0, 12)}…</span>
              <Link className="inline-flex items-center gap-1 text-accent" to={`/evidence/${alert.evidence_id}`}>
                Open evidence <ExternalLink className="h-3 w-3" />
              </Link>
            </div>
          </div>
        ) : (
          <p className="text-sm text-dim">No evidence bundle attached.</p>
        )}
      </div>

      {can("finding.review") ? (
        <div className="space-y-2 rounded-md border border-line bg-raised/40 p-3">
          <div className="card-t">Analyst decision</div>
          <textarea className="input h-16 resize-none" placeholder="Note (recorded in the audit chain)" value={note} onChange={(e) => setNote(e.target.value)} />
          <div className="flex flex-wrap gap-2">
            <button className="btn btn-primary" disabled={review.isPending} onClick={() => act("confirmed")}>
              <Check className="h-4 w-4" /> Confirm <span className="kbd">c</span>
            </button>
            <button className="btn" disabled={review.isPending} onClick={() => act("rejected")}>
              <X className="h-4 w-4" /> Reject <span className="kbd">r</span>
            </button>
            <button
              className="btn"
              disabled={review.isPending || !can("finding.escalate")}
              title={can("finding.escalate") ? "" : "Escalation needs the supervisor role"}
              onClick={() => act("escalated")}
            >
              <ChevronsUp className="h-4 w-4" /> Escalate <span className="kbd">e</span>
            </button>
          </div>
          {review.error ? <ErrorState error={review.error} /> : null}
        </div>
      ) : (
        <Caveat>Your role can read alerts but not record a decision.</Caveat>
      )}
      <Caveat>An alert records observed change and deviation from this site's own history. It carries no assessment of cause or intent.</Caveat>
    </div>
  );
}

export default function Alerts() {
  const [params, setParams] = useSearchParams();
  const [status, setStatus] = useState<ReviewStatus | "">("pending");
  const [maxP, setMaxP] = useState(4);
  const [site, setSite] = useState("");
  const sites = useSites();
  const q = useAlerts({ status, maxPriority: maxP, site: site || undefined });
  const focus = params.get("focus");

  const rows = useMemo(
    () => [...(q.data?.alerts ?? [])].sort((a, b) => a.priority - b.priority || b.created_at.localeCompare(a.created_at)),
    [q.data],
  );
  const selected = rows.find((a) => a.id === focus) ?? rows[0];

  // Arriving from a link to a reviewed alert should still show it.
  useEffect(() => {
    if (focus && q.data && !q.data.alerts.some((a) => a.id === focus) && status) setStatus("");
  }, [focus, q.data, status]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement || e.target instanceof HTMLSelectElement) return;
      const i = rows.findIndex((a) => a.id === selected?.id);
      if (e.key === "j" && i < rows.length - 1) setParams({ focus: rows[i + 1].id });
      if (e.key === "k" && i > 0) setParams({ focus: rows[i - 1].id });
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [rows, selected, setParams]);

  const counts = q.data?.queue;

  return (
    <div>
      <PageHeader
        title="Alert queue"
        subtitle="Ranked on severity, confidence, novelty, persistence and spatial reach. Deduplicated by place and kind."
        actions={<span className="text-xs text-dim"><span className="kbd">j</span>/<span className="kbd">k</span> move · <span className="kbd">c</span> confirm · <span className="kbd">r</span> reject · <span className="kbd">e</span> escalate</span>}
      />
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <select className="input w-auto" value={status} onChange={(e) => setStatus(e.target.value as ReviewStatus | "")} aria-label="Review status">
          <option value="pending">Pending</option>
          <option value="confirmed">Confirmed</option>
          <option value="escalated">Escalated</option>
          <option value="rejected">Rejected</option>
          <option value="">All statuses</option>
        </select>
        <select className="input w-auto" value={maxP} onChange={(e) => setMaxP(Number(e.target.value))} aria-label="Priority">
          {[1, 2, 3, 4].map((p) => (
            <option key={p} value={p}>
              P1{p > 1 ? `–P${p}` : " only"}
            </option>
          ))}
        </select>
        <select className="input w-auto" value={site} onChange={(e) => setSite(e.target.value)} aria-label="Site">
          <option value="">All sites</option>
          {sites.data?.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
        {counts && (
          <div className="ml-auto flex gap-2 text-xs">
            {[1, 2, 3, 4].map((p) => (
              <span key={p} className="flex items-center gap-1">
                <PriorityBadge p={p} /> {counts[`priority_${p}` as "priority_1"]}
              </span>
            ))}
          </div>
        )}
      </div>

      <div className="grid gap-4 lg:grid-cols-[minmax(320px,1fr)_1.3fr]">
        <Card bodyClass="p-0" className="max-h-[calc(100vh-220px)]">
          <Q q={q} empty={() => !rows.length && <Empty title="Nothing in this view">Change the filters, or the queue is clear.</Empty>}>
            {() => (
              <ul className="h-full divide-y divide-line overflow-y-auto">
                {rows.map((a) => (
                  <li key={a.id}>
                    <button
                      onClick={() => setParams({ focus: a.id })}
                      className={clsx(
                        "flex w-full items-start gap-3 border-l-2 px-3 py-2.5 text-left",
                        selected?.id === a.id ? "border-accent bg-raised" : "border-transparent hover:bg-raised/50",
                      )}
                    >
                      <PriorityBadge p={a.priority} />
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-sm font-medium">{a.title}</div>
                        <div className="truncate text-xs text-dim">{a.summary}</div>
                        <div className="mt-1 flex items-center gap-2 text-[11px] text-dim">
                          <span className="font-mono">{a.aoi_id}</span>
                          <span>{fmtDateTime(a.created_at)}</span>
                        </div>
                      </div>
                      <div className="flex flex-col items-end gap-1">
                        <SeverityBadge severity={a.severity} />
                        {a.review_status !== "pending" && <ReviewBadge status={a.review_status} />}
                      </div>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Q>
        </Card>
        <Card bodyClass="p-4 overflow-y-auto" className="max-h-[calc(100vh-220px)]">
          {selected ? <AlertDetail alert={selected} /> : <Empty title="Select an alert" />}
        </Card>
      </div>
    </div>
  );
}
