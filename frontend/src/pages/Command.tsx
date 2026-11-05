import { Activity, AlertOctagon, Boxes, Layers, MapPin, ShieldCheck } from "lucide-react";
import { useMemo } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useAlerts, useChanges, useHealth, useSites } from "../api/hooks";
import type { Alert, Change } from "../api/types";
import { WorldMap } from "../components/WorldMap";
import { Card, Empty, PageHeader, PriorityBadge, Q, ReviewBadge, SeverityBadge, Stat } from "../components/ui";
import { fmtArea, fmtDate, fmtNum, humanise } from "../lib/format";
import { usePalette } from "../lib/theme";
import { usePeriod } from "../state/period";

function weekOf(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`);
  const day = (d.getUTCDay() + 6) % 7;
  d.setUTCDate(d.getUTCDate() - day);
  return d.toISOString().slice(0, 10);
}

function ActivityChart({ changes }: { changes: Change[] }) {
  const pal = usePalette();
  const data = useMemo(() => {
    const m = new Map<string, Record<string, number | string>>();
    changes.forEach((c) => {
      const w = weekOf(c.detected_on);
      const row = m.get(w) ?? { week: w, critical: 0, high: 0, medium: 0, low: 0 };
      row[c.severity] = (row[c.severity] as number) + 1;
      m.set(w, row);
    });
    return [...m.values()].sort((a, b) => String(a.week).localeCompare(String(b.week)));
  }, [changes]);
  if (!data.length) return <Empty title="No changes in this window">Widen the analysis window in the top bar.</Empty>;
  return (
    <ResponsiveContainer width="100%" height={220}>
      <BarChart data={data} margin={{ top: 8, right: 8, left: -18, bottom: 0 }}>
        <CartesianGrid stroke={pal.line} strokeDasharray="2 4" vertical={false} />
        <XAxis dataKey="week" tick={{ fill: pal.dim, fontSize: 10 }} tickFormatter={(w) => fmtDate(w).slice(0, 6)} stroke={pal.line} />
        <YAxis tick={{ fill: pal.dim, fontSize: 10 }} stroke={pal.line} allowDecimals={false} />
        <Tooltip
          contentStyle={{ background: pal.panel, border: `1px solid ${pal.line}`, borderRadius: 6, fontSize: 12 }}
          labelFormatter={(w) => `Week of ${fmtDate(String(w))}`}
          cursor={{ fill: pal.raised }}
        />
        <Bar dataKey="low" stackId="s" fill={pal.low} />
        <Bar dataKey="medium" stackId="s" fill={pal.med} />
        <Bar dataKey="high" stackId="s" fill={pal.high} />
        <Bar dataKey="critical" stackId="s" fill={pal.crit} radius={[2, 2, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

function PriorityFeed({ alerts }: { alerts: Alert[] }) {
  const nav = useNavigate();
  const rows = [...alerts].sort((a, b) => a.priority - b.priority || b.created_at.localeCompare(a.created_at)).slice(0, 8);
  if (!rows.length) return <Empty title="Queue is clear">No alerts are waiting for review.</Empty>;
  return (
    <ul className="divide-y divide-line">
      {rows.map((a) => (
        <li key={a.id}>
          <button className="flex w-full items-start gap-3 px-4 py-2.5 text-left hover:bg-raised/60" onClick={() => nav(`/alerts?focus=${a.id}`)}>
            <PriorityBadge p={a.priority} />
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm font-medium">{a.title}</div>
              <div className="truncate text-xs text-dim">
                {a.aoi_id} · {fmtDate(a.created_at)} · {a.occurrences}× seen
              </div>
            </div>
            <SeverityBadge severity={a.severity} />
          </button>
        </li>
      ))}
    </ul>
  );
}

export default function Command() {
  const { window } = usePeriod();
  const sites = useSites();
  const health = useHealth();
  const alerts = useAlerts({ maxPriority: 4 });
  const changes = useChanges();
  const nav = useNavigate();

  const pending = alerts.data?.alerts.filter((a) => a.review_status === "pending") ?? [];
  const p1 = pending.filter((a) => a.priority === 1);
  const hot = new Set(pending.filter((a) => a.priority <= 2).map((a) => a.aoi_id));
  const changeList = changes.data ?? [];
  const significant = changeList.filter((c) => c.severity === "high" || c.severity === "critical");

  return (
    <div className="space-y-4">
      <PageHeader
        title="Command"
        subtitle={`Estate status for ${fmtDate(window.from)} – ${fmtDate(window.to)}`}
        actions={
          <>
            <Link className="btn" to="/copilot">Ask the copilot</Link>
            <Link className="btn btn-primary" to="/alerts">Open queue ({pending.length})</Link>
          </>
        }
      />

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <Stat label="Monitored sites" value={fmtNum(sites.data?.length)} sub={`${fmtNum(sites.data?.reduce((s, x) => s + x.area_km2, 0), 1)} km² under watch`} icon={<MapPin className="h-4 w-4" />} tone="accent" />
        <Stat label="P1 awaiting review" value={fmtNum(p1.length)} sub={`${pending.length} pending in total`} icon={<AlertOctagon className="h-4 w-4" />} tone={p1.length ? "crit" : "ink"} />
        <Stat label="Changes in window" value={fmtNum(changeList.length)} sub={`${significant.length} high or critical`} icon={<Activity className="h-4 w-4" />} tone="high" />
        <Stat label="Objects detected" value={fmtNum(health.data?.counts.detections)} sub="all time, all sites" icon={<Boxes className="h-4 w-4" />} tone="info" />
        <Stat label="Scenes ingested" value={fmtNum(health.data?.counts.scenes)} sub={`${fmtNum(health.data?.counts.evidence)} evidence bundles`} icon={<Layers className="h-4 w-4" />} />
        <Stat
          label="Audit chain"
          value={health.data ? (health.data.audit_chain_intact ? "Intact" : "Broken") : "—"}
          sub={`${fmtNum(health.data?.counts.audit_log)} entries, hash-chained`}
          icon={<ShieldCheck className="h-4 w-4" />}
          tone={health.data?.audit_chain_intact === false ? "crit" : "accent"}
        />
      </div>

      <div className="grid gap-4 xl:grid-cols-[1.6fr_1fr]">
        <Card title="Operational picture" actions={<Link to="/map" className="text-xs text-accent">Full map →</Link>} bodyClass="p-2">
          <Q q={sites}>
            {(s) => (
              <WorldMap
                height={380}
                points={s.map((x) => ({
                  id: x.id,
                  lon: x.centroid[0],
                  lat: x.centroid[1],
                  tone: hot.has(x.id) ? "critical" : "site",
                  pulse: hot.has(x.id),
                  label: x.name.split(" — ")[0],
                }))}
                onSelect={(id) => nav(`/sites/${id}`)}
              />
            )}
          </Q>
        </Card>
        <Card title="Priority feed" actions={<span className="text-xs text-dim">pending, by priority</span>} bodyClass="p-0">
          <Q q={alerts}>{() => <PriorityFeed alerts={pending} />}</Q>
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-[1.6fr_1fr]">
        <Card title="Change activity by week" actions={<span className="text-xs text-dim">stacked by severity</span>}>
          <Q q={changes}>{(c) => <ActivityChart changes={c} />}</Q>
        </Card>
        <Card title="Significant changes" bodyClass="p-0">
          <Q q={changes} empty={() => !significant.length && <Empty title="No high or critical changes in this window" />}>
            {() => (
              <ul className="max-h-[260px] divide-y divide-line overflow-y-auto">
                {significant.slice(0, 20).map((c) => (
                  <li key={c.id}>
                    <Link to={`/changes?focus=${c.id}`} className="flex items-center gap-3 px-4 py-2 hover:bg-raised/60">
                      <SeverityBadge severity={c.severity} />
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-sm">{humanise(c.type)} · {fmtArea(c.area_m2)}</div>
                        <div className="truncate text-xs text-dim">{c.aoi_id} · {fmtDate(c.detected_on)}</div>
                      </div>
                      <ReviewBadge status={c.review_status} />
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </Q>
        </Card>
      </div>

      <Card title="Site status" bodyClass="p-0">
        <Q q={sites}>
          {(s) => (
            <div className="overflow-x-auto">
              <table className="tbl">
                <thead>
                  <tr>
                    <th>Site</th>
                    <th>Kind</th>
                    <th className="text-right">Area</th>
                    <th className="text-right">Changes</th>
                    <th className="text-right">Pending alerts</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {s.map((x) => {
                    const n = changeList.filter((c) => c.aoi_id === x.id).length;
                    const pa = pending.filter((a) => a.aoi_id === x.id);
                    const status = pa.some((a) => a.priority === 1) ? "Anomalous" : pa.length ? "Watch" : "Normal";
                    return (
                      <tr key={x.id} className="row" onClick={() => nav(`/sites/${x.id}`)}>
                        <td>
                          <div className="font-medium">{x.name}</div>
                          <div className="mono text-dim">{x.id}</div>
                        </td>
                        <td>{humanise(x.kind)}</td>
                        <td className="text-right tabular-nums">{fmtNum(x.area_km2, 1)} km²</td>
                        <td className="text-right tabular-nums">{n}</td>
                        <td className="text-right tabular-nums">{pa.length}</td>
                        <td>
                          <span className={`chip ${status === "Anomalous" ? "border-crit/50 text-crit" : status === "Watch" ? "border-med/50 text-med" : "border-accent/40 text-accent"}`}>
                            {status}
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Q>
      </Card>
    </div>
  );
}
