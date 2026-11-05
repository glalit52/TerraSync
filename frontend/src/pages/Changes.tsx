import clsx from "clsx";
import { Check, ChevronsUp, X } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useChanges, useImagery, useReview, useSites } from "../api/hooks";
import type { Change, Scene } from "../api/types";
import { WorldMap } from "../components/WorldMap";
import { Card, Caveat, Empty, ErrorState, KeyValue, PageHeader, Q, ReviewBadge, SeverityBadge } from "../components/ui";
import { fmtArea, fmtDate, fmtPct, humanise } from "../lib/format";
import { useAuth } from "../state/auth";

function SceneCard({ label, scene, id }: { label: string; scene?: Scene; id: string }) {
  return (
    <div className="rounded-md border border-line p-3">
      <div className="card-t mb-1">{label}</div>
      {scene ? (
        <KeyValue
          rows={[
            ["Date", fmtDate(scene.acquired_on)],
            ["Platform", `${scene.constellation} · ${scene.sensor.toUpperCase()}`],
            ["GSD", `${scene.gsd_m} m`],
            ["Orbit", scene.orbit],
            ...(scene.sensor !== "sar" ? ([["Cloud", `${Math.round(scene.cloud_pct)}%`]] as [string, string][]) : []),
          ]}
        />
      ) : (
        <div className="mono break-all text-dim">{id}</div>
      )}
    </div>
  );
}

function ChangeViewer({ change }: { change: Change }) {
  const scenes = useImagery(change.aoi_id);
  const review = useReview();
  const { can } = useAuth();
  const [note, setNote] = useState("");
  const before = scenes.data?.find((s) => s.id === change.before_scene_id);
  const after = scenes.data?.find((s) => s.id === change.after_scene_id);
  const xs = change.geometry.map((p) => p[0]);
  const ys = change.geometry.map((p) => p[1]);
  const pad = 0.0015;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <SeverityBadge severity={change.severity} />
        <ReviewBadge status={change.review_status} />
        <span className="text-lg font-semibold">{humanise(change.type)}</span>
        <span className="text-sm text-dim">{fmtArea(change.area_m2)} · confidence {fmtPct(change.confidence)}</span>
      </div>
      <p className="text-sm">{change.explanation}</p>
      <div className="grid gap-3 md:grid-cols-2">
        <SceneCard label="Before" scene={before} id={change.before_scene_id} />
        <SceneCard label="After" scene={after} id={change.after_scene_id} />
      </div>
      <div>
        <div className="card-t mb-1.5">Change mask, in place</div>
        <WorldMap
          height={260}
          focus={[
            [Math.min(...xs) - pad, Math.min(...ys) - pad],
            [Math.max(...xs) + pad, Math.max(...ys) + pad],
          ]}
          polygons={[{ id: change.id, ring: change.geometry, tone: change.severity, label: humanise(change.type) }]}
        />
        <p className="mt-1 text-[11px] text-dim">
          The pixel chips (before, after, mask) live in the evidence pack. Pairs from different constellations, or SAR from different ground tracks,
          are refused by the engine rather than differenced.
        </p>
      </div>
      <div className="flex flex-wrap gap-2">
        <Link className="btn" to={`/evidence/${change.evidence_id}`}>Open evidence</Link>
        <Link className="btn" to={`/graph?site=${change.aoi_id}&node=${change.id}`}>Evidence path</Link>
        <Link className="btn" to={`/sites/${change.aoi_id}`}>Site page</Link>
      </div>
      {can("finding.review") && (
        <div className="space-y-2 rounded-md border border-line bg-raised/40 p-3">
          <div className="card-t">Analyst decision</div>
          <input className="input" placeholder="Note" value={note} onChange={(e) => setNote(e.target.value)} />
          <div className="flex gap-2">
            <button className="btn btn-primary" onClick={() => review.mutate({ id: change.id, status: "confirmed", note })}>
              <Check className="h-4 w-4" /> Confirm
            </button>
            <button className="btn" onClick={() => review.mutate({ id: change.id, status: "rejected", note })}>
              <X className="h-4 w-4" /> Reject
            </button>
            <button className="btn" disabled={!can("finding.escalate")} onClick={() => review.mutate({ id: change.id, status: "escalated", note })}>
              <ChevronsUp className="h-4 w-4" /> Escalate
            </button>
          </div>
          {review.error ? <ErrorState error={review.error} /> : null}
        </div>
      )}
    </div>
  );
}

export default function Changes() {
  const [params, setParams] = useSearchParams();
  const sites = useSites();
  const [site, setSite] = useState(params.get("site") ?? "");
  const [type, setType] = useState("");
  const [sev, setSev] = useState("");
  const [review, setReview] = useState("");
  const q = useChanges(site || undefined);
  const focus = params.get("focus");

  const rows = useMemo(
    () =>
      (q.data ?? [])
        .filter((c) => (!type || c.type === type) && (!sev || c.severity === sev) && (!review || c.review_status === review))
        .sort((a, b) => b.detected_on.localeCompare(a.detected_on)),
    [q.data, type, sev, review],
  );
  const types = [...new Set((q.data ?? []).map((c) => c.type))].sort();
  const selected = rows.find((c) => c.id === focus) ?? (q.data ?? []).find((c) => c.id === focus) ?? rows[0];

  return (
    <div>
      <PageHeader title="Change explorer" subtitle="Every detected change in the analysis window, with the scenes it was computed from." />
      <div className="mb-3 flex flex-wrap gap-2">
        <select className="input w-auto" value={site} onChange={(e) => setSite(e.target.value)} aria-label="Site">
          <option value="">All sites</option>
          {sites.data?.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
        <select className="input w-auto" value={type} onChange={(e) => setType(e.target.value)} aria-label="Type">
          <option value="">All types</option>
          {types.map((t) => <option key={t} value={t}>{humanise(t)}</option>)}
        </select>
        <select className="input w-auto" value={sev} onChange={(e) => setSev(e.target.value)} aria-label="Severity">
          <option value="">All severities</option>
          {["critical", "high", "medium", "low"].map((s) => <option key={s} value={s}>{humanise(s)}</option>)}
        </select>
        <select className="input w-auto" value={review} onChange={(e) => setReview(e.target.value)} aria-label="Review">
          <option value="">Any review state</option>
          {["pending", "confirmed", "rejected", "escalated"].map((s) => <option key={s} value={s}>{humanise(s)}</option>)}
        </select>
        <span className="ml-auto self-center text-xs text-dim">{rows.length} change(s)</span>
      </div>
      <div className="grid gap-4 xl:grid-cols-[1.2fr_1fr]">
        <Card bodyClass="p-0" className="max-h-[calc(100vh-220px)]">
          <Q q={q} empty={() => !rows.length && <Empty title="No changes match" />}>
            {() => (
              <div className="h-full overflow-auto">
                <table className="tbl">
                  <thead>
                    <tr>
                      <th>Detected</th>
                      <th>Type</th>
                      <th>Site</th>
                      <th className="text-right">Area</th>
                      <th>Sev.</th>
                      <th>Review</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((c) => (
                      <tr key={c.id} className={clsx("row", selected?.id === c.id && "bg-raised")} onClick={() => setParams({ focus: c.id })}>
                        <td className="whitespace-nowrap">{fmtDate(c.detected_on)}</td>
                        <td className="whitespace-nowrap">{humanise(c.type)}</td>
                        <td className="mono whitespace-nowrap text-dim">{c.aoi_id}</td>
                        <td className="whitespace-nowrap text-right tabular-nums">{fmtArea(c.area_m2)}</td>
                        <td><SeverityBadge severity={c.severity} /></td>
                        <td><ReviewBadge status={c.review_status} /></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Q>
        </Card>
        <Card bodyClass="p-4 overflow-y-auto" className="max-h-[calc(100vh-220px)]">
          {selected ? <ChangeViewer change={selected} /> : <Empty title="Select a change" />}
          <div className="mt-4">
            <Caveat>A change is observed difference between two scenes. It is not an assessment of who made it or why.</Caveat>
          </div>
        </Card>
      </div>
    </div>
  );
}
