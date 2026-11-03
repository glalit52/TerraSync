import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ComposedChart,
  Line,
  LineChart,
  ReferenceArea,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  useAlerts,
  useChanges,
  useCreateField,
  useEvents,
  useFields,
  useGraphStats,
  useImagery,
  useObjects,
  useObservations,
  useProjection,
  useRetireField,
  useSite,
  useTimeline,
} from "../api/hooks";
import type { SiteDetail as SiteDetailT } from "../api/types";
import { WorldMap } from "../components/WorldMap";
import {
  Card,
  Caveat,
  Empty,
  ErrorState,
  KeyValue,
  PageHeader,
  PriorityBadge,
  Q,
  SeverityBadge,
  Stat,
  Tabs,
  Tag,
} from "../components/ui";
import { fmtArea, fmtDate, fmtNum, fmtPct, humanise } from "../lib/format";
import { severityColor, usePalette } from "../lib/theme";
import { useAuth } from "../state/auth";
import { isoDay } from "../state/period";

type Tab = "overview" | "timeline" | "pattern" | "events" | "imagery" | "objects" | "fields" | "graph";

const tooltipStyle = (pal: ReturnType<typeof usePalette>) => ({
  background: pal.panel,
  border: `1px solid ${pal.line}`,
  borderRadius: 6,
  fontSize: 12,
  color: pal.ink,
});

function Overview({ site }: { site: SiteDetailT }) {
  const id = site.site.id;
  const changes = useChanges(id);
  const objects = useObjects(id);
  const events = useEvents(id);
  const alerts = useAlerts({ site: id, status: "pending" });
  const [showObjects, setShowObjects] = useState(false);
  const [sel, setSel] = useState<string | null>(null);
  const nav = useNavigate();
  const chg = changes.data ?? [];
  const selected = chg.find((c) => c.id === sel);

  // Only the latest scene's objects: plotting every pass stacks hundreds of
  // dots on the same berths and says nothing.
  const latestObjects = useMemo(() => {
    const o = objects.data ?? [];
    const last = o.reduce((m, x) => (x.observed_at > m ? x.observed_at : m), "");
    return o.filter((x) => x.observed_at === last);
  }, [objects.data]);

  return (
    <div className="grid gap-4 xl:grid-cols-[1.5fr_1fr]">
      <Card
        title="Site picture"
        bodyClass="p-2"
        actions={
          <label className="flex items-center gap-1.5 text-xs text-dim">
            <input type="checkbox" checked={showObjects} onChange={(e) => setShowObjects(e.target.checked)} /> Objects (latest pass)
          </label>
        }
      >
        <WorldMap
          height={440}
          focus={(() => {
            const xs = site.site.boundary.map((p) => p[0]);
            const ys = site.site.boundary.map((p) => p[1]);
            return [
              [Math.min(...xs), Math.min(...ys)],
              [Math.max(...xs), Math.max(...ys)],
            ];
          })()}
          selected={sel}
          onSelect={setSel}
          polygons={[
            { id: id, ring: site.site.boundary, tone: "site", label: site.site.name },
            ...chg.map((c) => ({ id: c.id, ring: c.geometry, tone: c.severity, label: `${humanise(c.type)} · ${fmtDate(c.detected_on)}` })),
          ]}
          points={
            showObjects
              ? latestObjects.map((o) => ({ id: o.id, lon: o.lon, lat: o.lat, tone: "object" as const, label: `${humanise(o.class)} ${fmtPct(o.confidence)}` }))
              : []
          }
          overlay={
            selected ? (
              <div className="card absolute bottom-2 left-2 max-w-sm p-3 text-sm shadow-xl">
                <div className="flex items-center gap-2">
                  <SeverityBadge severity={selected.severity} />
                  <span className="font-medium">{humanise(selected.type)}</span>
                  <span className="text-xs text-dim">{fmtArea(selected.area_m2)}</span>
                </div>
                <p className="mt-1 text-xs text-dim">{selected.explanation}</p>
                <div className="mt-2 flex gap-2">
                  <button className="btn text-xs" onClick={() => nav(`/changes?focus=${selected.id}`)}>Change viewer</button>
                  <button className="btn text-xs" onClick={() => nav(`/evidence/${selected.evidence_id}`)}>Evidence</button>
                </div>
              </div>
            ) : null
          }
        />
      </Card>
      <div className="space-y-4">
        <Card title="Events (fused findings)" bodyClass="p-0">
          <Q q={events} empty={(e) => !e.events.length && <Empty title="No events in this window" />}>
            {(e) => (
              <>
                <div className="border-b border-line px-4 py-2 text-xs text-dim">
                  {e.summary.findings} findings fused into <span className="text-ink">{e.summary.events} events</span> — the queue is{" "}
                  <span className="text-accent">{fmtPct(e.summary.reduction)}</span> shorter than the raw feed.
                </div>
                <ul className="divide-y divide-line">
                  {e.events.slice(0, 6).map((ev) => (
                    <li key={ev.id} className="px-4 py-2">
                      <div className="flex items-center gap-2">
                        <SeverityBadge severity={ev.severity} />
                        <span className="truncate text-sm">{humanise(ev.kind)}</span>
                        <span className="ml-auto text-xs text-dim">{ev.looks} looks · {ev.trajectory}</span>
                      </div>
                      <div className="mt-0.5 text-xs text-dim">
                        {fmtDate(ev.started_on)} → {fmtDate(ev.last_seen_on)}
                      </div>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </Q>
        </Card>
        <Card title="Pending alerts" bodyClass="p-0">
          <Q q={alerts} empty={(a) => !a.alerts.length && <Empty title="No pending alerts" />}>
            {(a) => (
              <ul className="divide-y divide-line">
                {a.alerts.slice(0, 6).map((x) => (
                  <li key={x.id}>
                    <Link to={`/alerts?focus=${x.id}`} className="flex items-center gap-2 px-4 py-2 hover:bg-raised/50">
                      <PriorityBadge p={x.priority} />
                      <span className="truncate text-sm">{x.title}</span>
                      <span className="ml-auto text-xs text-dim">{fmtDate(x.created_at)}</span>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </Q>
        </Card>
      </div>
    </div>
  );
}

function TimelineTab({ id }: { id: string }) {
  const pal = usePalette();
  const t = useTimeline(id);
  return (
    <Q q={t} empty={(d) => !d.acquisitions.length && <Empty title="No acquisitions in this window">Widen the analysis window.</Empty>}>
      {(d) => {
        const days = new Map<string, { date: string; score?: number; change?: number; sev?: string }>();
        d.acquisitions.forEach((a) => days.set(a.date, { date: a.date }));
        d.anomaly_scores.forEach((a) => days.set(a.date, { ...(days.get(a.date) ?? { date: a.date }), score: a.score }));
        const series = [...days.values()].sort((a, b) => a.date.localeCompare(b.date));
        const changes = d.changes.map((c) => ({ date: c.date, change: Math.log10(Math.max(10, c.area_m2)), sev: c.severity, area: c.area_m2, type: c.type }));
        const usable = d.acquisitions.filter((a) => a.usable).length;
        return (
          <div className="space-y-4">
            <Card title="Anomaly score and changes" actions={<span className="text-xs text-dim">score 0–100 · changes sized by log area</span>}>
              <ResponsiveContainer width="100%" height={280}>
                <ComposedChart margin={{ top: 8, right: 16, left: -12, bottom: 0 }}>
                  <CartesianGrid stroke={pal.line} strokeDasharray="2 4" />
                  <XAxis dataKey="date" type="category" allowDuplicatedCategory={false} tick={{ fill: pal.dim, fontSize: 10 }} stroke={pal.line} tickFormatter={(x) => fmtDate(x).slice(0, 6)} />
                  <YAxis yAxisId="s" domain={[0, 100]} tick={{ fill: pal.dim, fontSize: 10 }} stroke={pal.line} />
                  <YAxis yAxisId="c" orientation="right" hide domain={[0, 7]} />
                  <ReferenceArea yAxisId="s" y1={65} y2={100} fill={pal.crit} fillOpacity={0.06} />
                  <Tooltip contentStyle={tooltipStyle(pal)} />
                  <Line yAxisId="s" data={series} dataKey="score" stroke={pal.accent} strokeWidth={2} dot={{ r: 2 }} connectNulls name="anomaly score" />
                  <Scatter yAxisId="c" data={changes} dataKey="change" name="change" shape={(p: { cx?: number; cy?: number; payload?: { sev: string } }) => (
                    <circle cx={p.cx} cy={p.cy} r={4} fill={severityColor(pal, p.payload?.sev ?? "low")} fillOpacity={0.7} />
                  )} />
                </ComposedChart>
              </ResponsiveContainer>
              <p className="mt-2 text-xs text-dim">The shaded band is where the default deviation rule fires (score ≥ 65).</p>
            </Card>
            <Card title={`Acquisitions — ${usable} of ${d.acquisitions.length} usable`}>
              <div className="flex flex-wrap gap-1">
                {d.acquisitions.map((a, i) => (
                  <div
                    key={i}
                    title={`${a.date} · ${a.constellation} · ${a.usable ? "usable" : a.reason}`}
                    className={`h-6 w-3 rounded-sm ${a.usable ? (a.sensor === "sar" ? "bg-info" : "bg-accent") : "bg-line"}`}
                  />
                ))}
              </div>
              <div className="mt-2 flex gap-4 text-xs text-dim">
                <span><span className="mr-1 inline-block h-2 w-2 rounded-sm bg-accent" />optical</span>
                <span><span className="mr-1 inline-block h-2 w-2 rounded-sm bg-info" />SAR</span>
                <span><span className="mr-1 inline-block h-2 w-2 rounded-sm bg-line" />rejected (cloud, sun, quality)</span>
              </div>
              <Caveat>Rejected scenes are shown on purpose: a timeline with only findings cannot tell a quiet month from a cloudy one.</Caveat>
            </Card>
          </div>
        );
      }}
    </Q>
  );
}

function PatternTab({ id }: { id: string }) {
  const pal = usePalette();
  const obs = useObservations(id);
  const [metric, setMetric] = useState("");
  const m = metric || obs.data?.metrics.find((x) => (obs.data?.series[x] ?? []).some((p) => p.value > 0)) || obs.data?.metrics[0] || "";
  const series = obs.data?.series[m] ?? [];
  const lastDate = series.at(-1)?.date;
  const [horizon, setHorizon] = useState("");
  const defaultHorizon = lastDate ? isoDay(new Date(Date.parse(lastDate) + 14 * 86_400_000)) : "";
  const proj = useProjection(id, m, horizon || defaultHorizon);

  return (
    <Q q={obs} empty={(d) => !d.metrics.length && <Empty title="No measurements in this window" />}>
      {(d) => (
        <div className="grid gap-4 xl:grid-cols-[1.6fr_1fr]">
          <Card
            title="Measured series"
            actions={
              <select className="input w-auto py-1 text-xs" value={m} onChange={(e) => setMetric(e.target.value)}>
                {d.metrics.map((x) => (
                  <option key={x} value={x}>
                    {humanise(x)}
                  </option>
                ))}
              </select>
            }
          >
            <ResponsiveContainer width="100%" height={280}>
              <LineChart data={series} margin={{ top: 8, right: 16, left: -12, bottom: 0 }}>
                <CartesianGrid stroke={pal.line} strokeDasharray="2 4" />
                <XAxis dataKey="date" tick={{ fill: pal.dim, fontSize: 10 }} stroke={pal.line} tickFormatter={(x) => fmtDate(x).slice(0, 6)} />
                <YAxis tick={{ fill: pal.dim, fontSize: 10 }} stroke={pal.line} />
                <Tooltip contentStyle={tooltipStyle(pal)} />
                <Line dataKey="value" stroke={pal.info} strokeWidth={2} dot={{ r: 2.5 }} name={humanise(m)} />
              </LineChart>
            </ResponsiveContainer>
            <p className="mt-2 text-xs text-dim">One point per usable pass. The baseline is a robust median with a weekday term, held per site.</p>
          </Card>
          <Card title="Projection">
            <label className="block">
              <span className="label">Project to</span>
              <input type="date" className="input" value={horizon || defaultHorizon} onChange={(e) => setHorizon(e.target.value)} />
            </label>
            <div className="mt-3">
              {proj.isLoading ? null : proj.error ? (
                <ErrorState error={proj.error} />
              ) : proj.data?.is_projection && proj.data.band ? (
                <div className="space-y-2">
                  <p className="text-sm">{String(proj.data.headline ?? "")}</p>
                  <KeyValue
                    rows={[
                      ["Range", `${fmtNum(proj.data.band.low, 1)} – ${fmtNum(proj.data.band.high, 1)}`],
                      ["Confidence", `${proj.data.band.confidence_pct}%`],
                      ["Days ahead", fmtNum(proj.data.days_ahead)],
                    ]}
                  />
                  {proj.data.assumption && <Caveat>{proj.data.assumption}</Caveat>}
                </div>
              ) : proj.data ? (
                <Caveat>
                  <span className="font-medium text-med">Not projected. </span>
                  {String(proj.data.reason ?? proj.data.refused ?? "The series cannot support a projection.")}
                </Caveat>
              ) : null}
            </div>
            <p className="mt-3 text-[11px] text-dim">
              The server refuses under six observations, a straight-line fit below R² 0.30, or a horizon more than half the observed span ahead.
            </p>
          </Card>
        </div>
      )}
    </Q>
  );
}

function EventsTab({ id }: { id: string }) {
  const e = useEvents(id);
  return (
    <Q q={e} empty={(d) => !d.events.length && <Empty title="No events in this window" />}>
      {(d) => (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="Raw findings" value={d.summary.findings} />
            <Stat label="Events" value={d.summary.events} tone="accent" />
            <Stat label="Queue reduction" value={fmtPct(d.summary.reduction)} tone="info" />
            <Stat label="Single look" value={d.summary.single_look} sub="need a second pass" />
          </div>
          <Card bodyClass="p-0">
            <div className="overflow-x-auto">
              <table className="tbl">
                <thead>
                  <tr>
                    <th>Event</th>
                    <th>Severity</th>
                    <th>Period</th>
                    <th className="text-right">Looks</th>
                    <th>Trajectory</th>
                    <th className="text-right">Peak area</th>
                    <th className="text-right">Novelty</th>
                    <th className="text-right">Confidence</th>
                  </tr>
                </thead>
                <tbody>
                  {d.events.map((ev) => (
                    <tr key={ev.id}>
                      <td>
                        <div className="font-medium">{humanise(ev.kind)}</div>
                        <div className="max-w-md text-xs text-dim">{ev.headline}</div>
                      </td>
                      <td><SeverityBadge severity={ev.severity} /></td>
                      <td className="whitespace-nowrap text-xs">{fmtDate(ev.started_on)} → {fmtDate(ev.last_seen_on)}</td>
                      <td className="text-right">{ev.looks}</td>
                      <td>{ev.trajectory}</td>
                      <td className="text-right">{fmtArea(ev.peak_area_m2)}</td>
                      <td className="text-right">{fmtNum(ev.novelty, 2)}</td>
                      <td className="text-right">{fmtPct(ev.confidence)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
          <Caveat>Severity is the worst look, never an average. Repetition raises confidence but is capped, because a systematic error repeats too.</Caveat>
        </div>
      )}
    </Q>
  );
}

function ImageryTab({ id }: { id: string }) {
  const s = useImagery(id);
  return (
    <Card bodyClass="p-0">
      <Q q={s} empty={(d) => !d.length && <Empty title="No scenes in this window" />}>
        {(d) => (
          <div className="max-h-[560px] overflow-auto">
            <table className="tbl">
              <thead>
                <tr>
                  <th>Acquired</th>
                  <th>Constellation</th>
                  <th>Sensor</th>
                  <th className="text-right">GSD</th>
                  <th className="text-right">Cloud</th>
                  <th className="text-right">Sun elev.</th>
                  <th>Orbit</th>
                  <th>Usable</th>
                </tr>
              </thead>
              <tbody>
                {[...d].reverse().map((x) => (
                  <tr key={x.id}>
                    <td className="whitespace-nowrap">{fmtDate(x.acquired_on)}</td>
                    <td>{x.constellation}</td>
                    <td className="uppercase">{x.sensor}</td>
                    <td className="text-right">{x.gsd_m} m</td>
                    <td className="text-right">{x.sensor === "sar" ? "n/a" : `${fmtNum(x.cloud_pct)}%`}</td>
                    <td className="text-right">{fmtNum(x.sun_elevation_deg, 1)}°</td>
                    <td className="font-mono text-xs">{x.orbit}</td>
                    <td>{x.usable ? <span className="text-accent">yes</span> : <span className="text-dim">no — {x.unusable_reason}</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Q>
    </Card>
  );
}

function ObjectsTab({ id }: { id: string }) {
  const pal = usePalette();
  const o = useObjects(id);
  return (
    <Q q={o} empty={(d) => !d.length && <Empty title="No detections">Detection depends on resolution: see Sensing limits.</Empty>}>
      {(d) => {
        const byClass = Object.entries(
          d.reduce<Record<string, number>>((m, x) => ((m[x.class] = (m[x.class] ?? 0) + 1), m), {}),
        )
          .map(([k, v]) => ({ cls: humanise(k), n: v }))
          .sort((a, b) => b.n - a.n);
        const models = [...new Set(d.map((x) => x.model_version))];
        return (
          <div className="grid gap-4 xl:grid-cols-[1fr_1.4fr]">
            <Card title="Detections by class" actions={<span className="text-xs text-dim">model {models.join(", ")}</span>}>
              <ResponsiveContainer width="100%" height={Math.max(160, byClass.length * 30)}>
                <BarChart data={byClass} layout="vertical" margin={{ left: 20, right: 16 }}>
                  <XAxis type="number" tick={{ fill: pal.dim, fontSize: 10 }} stroke={pal.line} />
                  <YAxis type="category" dataKey="cls" tick={{ fill: pal.ink, fontSize: 11 }} width={120} stroke={pal.line} />
                  <Tooltip contentStyle={tooltipStyle(pal)} cursor={{ fill: pal.raised }} />
                  <Bar dataKey="n" fill={pal.info} radius={[0, 3, 3, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </Card>
            <Card title={`Detections (${d.length})`} bodyClass="p-0">
              <div className="max-h-[420px] overflow-auto">
                <table className="tbl">
                  <thead>
                    <tr>
                      <th>Class</th>
                      <th className="text-right">Conf.</th>
                      <th className="text-right">Extent</th>
                      <th>Observed</th>
                      <th>Scene</th>
                    </tr>
                  </thead>
                  <tbody>
                    {d.slice(0, 300).map((x) => (
                      <tr key={x.id}>
                        <td>{humanise(x.class)}</td>
                        <td className="text-right">{fmtPct(x.confidence)}</td>
                        <td className="text-right">{fmtNum(x.extent_m, 1)} m</td>
                        <td className="whitespace-nowrap">{fmtDate(x.observed_at)}</td>
                        <td className="mono text-dim">{x.scene_id.split(":")[0]}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          </div>
        );
      }}
    </Q>
  );
}

function FieldsTab({ id }: { id: string }) {
  const f = useFields(id);
  const create = useCreateField(id);
  const retire = useRetireField(id);
  const { can } = useAuth();
  const [name, setName] = useState("");
  const [use, setUse] = useState("");
  const [geo, setGeo] = useState("");
  const [err, setErr] = useState("");
  return (
    <div className="grid gap-4 xl:grid-cols-[1.4fr_1fr]">
      <Card title="Fields and sub-areas" bodyClass="p-0">
        <Q q={f} empty={(d) => !d.fields.length && <Empty title="No fields">Fields name parts of a site (Block D, Berth 4) so findings can be located by name.</Empty>}>
          {(d) => (
            <table className="tbl">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Use</th>
                  <th className="text-right">Hectares</th>
                  <th>State</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {d.fields.map((x) => (
                  <tr key={x.id}>
                    <td>{x.name}</td>
                    <td>{x.use}</td>
                    <td className="text-right">{fmtNum(x.hectares, 2)}</td>
                    <td>{x.active ? <Tag className="text-accent">active</Tag> : <Tag>retired</Tag>}</td>
                    <td className="text-right">
                      {x.active && can("field.update") && (
                        <button className="btn text-xs" onClick={() => retire.mutate(x.id)}>Retire</button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Q>
      </Card>
      {can("field.create") && (
        <Card title="Add field">
          <div className="space-y-2">
            <input className="input" placeholder="Name" value={name} onChange={(e) => setName(e.target.value)} />
            <input className="input" placeholder="Use (e.g. berth, array block)" value={use} onChange={(e) => setUse(e.target.value)} />
            <textarea className="input h-28 font-mono text-xs" placeholder="GeoJSON polygon, or [[lon,lat],...]" value={geo} onChange={(e) => setGeo(e.target.value)} />
            {err && <p className="text-xs text-crit">{err}</p>}
            {create.error ? <ErrorState error={create.error} /> : null}
            <button
              className="btn btn-primary"
              disabled={!name || !geo || create.isPending}
              onClick={() => {
                try {
                  const boundary = JSON.parse(geo);
                  setErr("");
                  create.mutate({ name, use, notes: "", boundary }, { onSuccess: () => (setName(""), setUse(""), setGeo("")) });
                } catch (e) {
                  setErr(`Not valid JSON: ${(e as Error).message}`);
                }
              }}
            >
              Add field
            </button>
          </div>
        </Card>
      )}
    </div>
  );
}

function GraphTab({ id }: { id: string }) {
  const g = useGraphStats(id);
  return (
    <Q q={g}>
      {(d) => (
        <div className="grid gap-4 md:grid-cols-2">
          <Card title={`Nodes (${d.stats.nodes})`}>
            <KeyValue rows={Object.entries(d.stats.by_kind).map(([k, v]) => [humanise(k), fmtNum(v)])} />
          </Card>
          <Card title={`Edges (${d.stats.edges})`}>
            <KeyValue rows={Object.entries(d.stats.by_relation).map(([k, v]) => [humanise(k), fmtNum(v)])} />
            <Link className="btn btn-primary mt-4" to={`/graph?site=${id}`}>Open graph explorer</Link>
          </Card>
        </div>
      )}
    </Q>
  );
}

export default function SiteDetail() {
  const { id } = useParams();
  const site = useSite(id);
  const changes = useChanges(id);
  const [tab, setTab] = useState<Tab>("overview");

  return (
    <Q q={site}>
      {(s) => {
        const pending = (changes.data ?? []).filter((c) => c.review_status === "pending").length;
        const status = s.peak_anomaly_score >= 65 ? "Anomalous" : s.alerts > 0 ? "Watch" : "Normal";
        return (
          <div className="space-y-4">
            <PageHeader
              crumbs={<><Link to="/sites" className="hover:underline">Sites</Link> / {s.site.id}</>}
              title={s.site.name}
              subtitle={`${humanise(s.site.kind)} · ${fmtNum(s.site.area_km2, 2)} km² · fingerprint ${s.site.fingerprint}`}
              actions={
                <>
                  <span className={`chip px-2 py-1 text-xs ${status === "Anomalous" ? "border-crit/50 text-crit" : status === "Watch" ? "border-med/50 text-med" : "border-accent/50 text-accent"}`}>
                    {status}
                  </span>
                  <Link className="btn" to={`/copilot?q=${encodeURIComponent(`what changed at ${s.site.name.split(" — ")[0]} in the last 30 days`)}`}>Ask copilot</Link>
                </>
              }
            />
            <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
              <Stat label="Scenes acquired" value={s.coverage.acquired} sub={`${s.coverage.usable} usable`} />
              <Stat label="Coverage" value={s.coverage.acquired ? fmtPct(s.coverage.usable / s.coverage.acquired) : "—"} sub="usable / acquired" tone="info" />
              <Stat label="Changes" value={s.changes} sub={`${pending} pending review`} tone="high" />
              <Stat label="Peak anomaly" value={fmtNum(s.peak_anomaly_score, 1)} sub="0–100, vs own baseline" tone={s.peak_anomaly_score >= 65 ? "crit" : "ink"} />
              <Stat label="Alerts" value={s.alerts} tone="med" />
            </div>
            <Tabs<Tab>
              value={tab}
              onChange={setTab}
              tabs={[
                { id: "overview", label: "Overview" },
                { id: "timeline", label: "Timeline" },
                { id: "pattern", label: "Pattern of life" },
                { id: "events", label: "Events" },
                { id: "imagery", label: "Imagery" },
                { id: "objects", label: "Objects" },
                { id: "fields", label: "Fields" },
                { id: "graph", label: "Graph" },
              ]}
            />
            {tab === "overview" && <Overview site={s} />}
            {tab === "timeline" && <TimelineTab id={s.site.id} />}
            {tab === "pattern" && <PatternTab id={s.site.id} />}
            {tab === "events" && <EventsTab id={s.site.id} />}
            {tab === "imagery" && <ImageryTab id={s.site.id} />}
            {tab === "objects" && <ObjectsTab id={s.site.id} />}
            {tab === "fields" && <FieldsTab id={s.site.id} />}
            {tab === "graph" && <GraphTab id={s.site.id} />}
          </div>
        );
      }}
    </Q>
  );
}
