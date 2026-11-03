import { useQueries, useQuery } from "@tanstack/react-query";
import { Download, FileUp, MapPinned, PenLine, Printer, Undo2 } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useSites } from "../api/hooks";
import type { Change, Field, LonLat, SiteDetail, SiteSummary } from "../api/types";
import { WorldMap, type MapPoint, type MapPolygon } from "../components/WorldMap";
import { Card, Caveat, Empty, ErrorState, KeyValue, Loading, PageHeader, ReviewBadge, SeverityBadge, Stat, Tabs } from "../components/ui";
import {
  centroid,
  circleRing,
  DAMAGE_TYPES,
  parseAssetsCsv,
  pointInPolygon,
  SAMPLE_PORTFOLIO,
  SCORE_METHOD,
  screenAsset,
  toCsv,
  type Asset,
  type Screening,
} from "../lib/assets";
import { fmtArea, fmtDate, fmtLonLat, fmtNum, fmtPct, humanise } from "../lib/format";
import { usePeriod } from "../state/period";

// Asset intelligence: the monitoring record read from the point of view of a
// place someone owns, insures or is responsible for. Three questions:
//   - what has been seen at and around this location?
//   - across a portfolio, which assets have the most observed change nearby?
//   - after an event, which findings and assets fall inside its footprint?

/** Every change in the window, unpaged, with the server's own truncation flag. */
function useAllChanges() {
  const { window } = usePeriod();
  return useQuery({
    queryKey: ["changes-all", window.from, window.to],
    queryFn: () =>
      api<{ changes: Change[]; truncated: boolean; limit: number }>("/api/changes", {
        query: { from: window.from, to: window.to, limit: 5000 },
      }),
  });
}

/** Site boundaries, so coverage is decided by the polygon and not its bounding box. */
function useBoundaries(sites: SiteSummary[] | undefined) {
  const results = useQueries({
    queries: (sites ?? []).map((s) => ({
      queryKey: ["site-boundary", s.id],
      queryFn: () => api<SiteDetail>(`/api/sites/${encodeURIComponent(s.id)}`),
      staleTime: 600_000,
    })),
  });
  return useMemo(
    () => results.flatMap((r) => (r.data ? [{ id: r.data.site.id, name: r.data.site.name, ring: r.data.site.boundary }] : [])),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [results.map((r) => r.dataUpdatedAt).join(",")],
  );
}

type Boundary = { id: string; name: string; ring: LonLat[] };

function coveringSite(p: LonLat, boundaries: Boundary[]): Boundary | undefined {
  return boundaries.find((b) => b.ring.length >= 3 && pointInPolygon(p, b.ring));
}

function scoreTone(score: number): "crit" | "high" | "med" | "ink" {
  return score >= 70 ? "crit" : score >= 40 ? "high" : score >= 15 ? "med" : "ink";
}

const TONE_TEXT = { crit: "text-crit", high: "text-high", med: "text-med", ink: "text-ink" } as const;

function TruncationNotice({ truncated, limit }: { truncated?: boolean; limit?: number }) {
  if (!truncated) return null;
  return (
    <div className="rounded-md border border-crit/40 bg-crit/5 p-3 text-sm text-crit">
      The server returned its maximum of {limit} findings for this window, so some were not considered. Shorten the analysis window and screen
      again; assets near the omitted findings would otherwise read as quieter than they are.
    </div>
  );
}

// ---------------------------------------------------------------------------
// 1. Location report
// ---------------------------------------------------------------------------

function LocationReport({ boundaries }: { boundaries: Boundary[] }) {
  const { window } = usePeriod();
  const all = useAllChanges();
  const [lon, setLon] = useState("69.7028");
  const [lat, setLat] = useState("22.7430");
  const [radius, setRadius] = useState(500);
  const p: LonLat = [Number(lon), Number(lat)];
  const valid = Number.isFinite(p[0]) && Number.isFinite(p[1]) && Math.abs(p[0]) <= 180 && Math.abs(p[1]) <= 90;
  const site = valid ? coveringSite(p, boundaries) : undefined;
  const field = useQuery({
    queryKey: ["field-at", lon, lat, site?.id],
    queryFn: () => api<{ field: Field | null }>("/api/fields/at", { query: { lon, lat, aoi_id: site?.id } }),
    enabled: valid && !!site,
  });

  const s: Screening | null = useMemo(
    () => (valid && all.data ? screenAsset({ id: "point", name: "Selected location", lon: p[0], lat: p[1] }, all.data.changes, radius) : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [valid, all.data, lon, lat, radius],
  );

  // Framed on the search radius, so the findings that count are large enough to see.
  const circle = valid ? circleRing(p, radius) : [p];
  const focus: [[number, number], [number, number]] = [
    [Math.min(...circle.map((q) => q[0])), Math.min(...circle.map((q) => q[1]))],
    [Math.max(...circle.map((q) => q[0])), Math.max(...circle.map((q) => q[1]))],
  ];

  return (
    <div className="space-y-4">
      <div className="grid gap-4 xl:grid-cols-[340px_1fr]">
        <Card title="Location" className="print:hidden">
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-2">
              <label>
                <span className="label">Longitude</span>
                <input className="input font-mono" value={lon} onChange={(e) => setLon(e.target.value)} />
              </label>
              <label>
                <span className="label">Latitude</span>
                <input className="input font-mono" value={lat} onChange={(e) => setLat(e.target.value)} />
              </label>
            </div>
            <label className="block">
              <span className="label">Search radius: {radius} m</span>
              <input type="range" min={50} max={3000} step={50} value={radius} onChange={(e) => setRadius(Number(e.target.value))} className="w-full" />
            </label>
            <p className="text-xs text-dim">Or click the map to move the point. The report uses the analysis window in the top bar.</p>
            <button className="btn w-full justify-center" onClick={() => globalThis.print()} disabled={!s}>
              <Printer className="h-4 w-4" /> Print / save as PDF
            </button>
          </div>
        </Card>
        <Card bodyClass="p-2">
          <WorldMap
            height={380}
            focus={focus}
            drawing
            onDrawPoint={([x, y]) => (setLon(x.toFixed(5)), setLat(y.toFixed(5)))}
            polygons={[
              ...(site ? [{ id: site.id, ring: site.ring, tone: "site" as const, label: site.name }] : []),
              ...(valid ? [{ id: "radius", ring: circleRing(p, radius), tone: "draft" as const, label: `${radius} m search radius` }] : []),
              ...(s?.nearby.map(({ change: c }) => ({ id: c.id, ring: c.geometry, tone: c.severity, label: `${humanise(c.type)} · ${fmtDate(c.detected_on)}` })) ?? []),
            ]}
            points={valid ? [{ id: "pt", lon: p[0], lat: p[1], tone: "sat", size: 6, pulse: true, label: "Selected location" }] : []}
          />
        </Card>
      </div>

      {!valid ? (
        <ErrorState error={new Error("Enter a longitude between −180 and 180 and a latitude between −90 and 90.")} />
      ) : all.isLoading ? (
        <Loading label="Reading the record" />
      ) : all.error ? (
        <ErrorState error={all.error} />
      ) : s ? (
        <div className="space-y-4">
          <TruncationNotice truncated={all.data?.truncated} limit={all.data?.limit} />
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="Observed-change score" value={s.score} sub="0–100, see method" tone={scoreTone(s.score)} />
            <Stat label="Findings in radius" value={s.count} sub={`${s.confirmed} confirmed`} tone="high" />
            <Stat label="Nearest finding" value={s.nearestM === null ? "—" : s.nearestM === 0 ? "On site" : `${fmtNum(s.nearestM)} m`} />
            <Stat label="Worst severity" value={s.worst ? humanise(s.worst) : "None"} tone={s.worst === "critical" ? "crit" : s.worst === "high" ? "high" : "ink"} />
          </div>
          <Card title="Location report">
            <KeyValue
              rows={[
                ["Location", <span key="l" className="font-mono">{fmtLonLat(p[0], p[1])}</span>],
                ["Period", `${fmtDate(window.from)} – ${fmtDate(window.to)}`],
                [
                  "Monitoring",
                  site ? (
                    <Link key="s" className="text-accent hover:underline" to={`/sites/${site.id}`}>{site.name}</Link>
                  ) : (
                    <span key="s" className="text-med">Not inside any monitored site — absence of findings here means unobserved, not unchanged.</span>
                  ),
                ],
                ...(site ? ([["Field", field.data?.field ? `${field.data.field.name}${field.data.field.use ? ` (${field.data.field.use})` : ""}` : "No named field at this point"]] as [string, string][]) : []),
                ["Summary", s.reasons.length ? s.reasons.join("; ") : `No findings within ${radius} m in this period.`],
              ]}
            />
          </Card>
          {s.nearby.length > 0 && (
            <Card title="Findings near this location" bodyClass="p-0">
              <div className="overflow-x-auto">
                <table className="tbl">
                  <thead>
                    <tr>
                      <th>Detected</th>
                      <th>Type</th>
                      <th className="text-right">Distance</th>
                      <th className="text-right">Area</th>
                      <th>Severity</th>
                      <th>Review</th>
                      <th>Evidence</th>
                    </tr>
                  </thead>
                  <tbody>
                    {s.nearby.slice(0, 50).map(({ change: c, distanceM: d }) => (
                      <tr key={c.id}>
                        <td className="whitespace-nowrap">{fmtDate(c.detected_on)}</td>
                        <td className="whitespace-nowrap">{humanise(c.type)}</td>
                        <td className="text-right tabular-nums">{d === 0 ? "on site" : `${fmtNum(d)} m`}</td>
                        <td className="whitespace-nowrap text-right">{fmtArea(c.area_m2)}</td>
                        <td><SeverityBadge severity={c.severity} /></td>
                        <td><ReviewBadge status={c.review_status} /></td>
                        <td><Link className="text-accent hover:underline" to={`/evidence/${c.evidence_id}`}>open</Link></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          )}
          <Caveat>{SCORE_METHOD}</Caveat>
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 2. Portfolio screening
// ---------------------------------------------------------------------------

function download(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

function PortfolioScreening({
  boundaries,
  csv,
  setCsv,
}: {
  boundaries: Boundary[];
  csv: string;
  setCsv: (s: string) => void;
}) {
  const { window } = usePeriod();
  const all = useAllChanges();
  const [radius, setRadius] = useState(500);
  const [selected, setSelected] = useState<string | null>(null);
  const parsed = useMemo(() => parseAssetsCsv(csv), [csv]);

  const rows = useMemo(() => {
    if (!all.data) return [];
    return parsed.assets
      .map((a) => ({ s: screenAsset(a, all.data.changes, radius), site: coveringSite([a.lon, a.lat], boundaries) }))
      // Ties: monitored before unmonitored, because a 0 from an observed site
      // is a finding and a 0 from an unobserved one is the absence of one.
      .sort((x, y) => y.s.score - x.s.score || Number(!!y.site) - Number(!!x.site) || (y.s.asset.value ?? 0) - (x.s.asset.value ?? 0));
  }, [parsed, all.data, radius, boundaries]);

  const covered = rows.filter((r) => r.site);
  const withFindings = rows.filter((r) => r.s.count > 0);
  const exposedValue = rows.filter((r) => r.s.worst === "high" || r.s.worst === "critical").reduce((t, r) => t + (r.s.asset.value ?? 0), 0);
  const totalValue = rows.reduce((t, r) => t + (r.s.asset.value ?? 0), 0);
  const sel = rows.find((r) => r.s.asset.id === selected);

  const exportRows = () =>
    download(
      `terrashield-screening-${window.from}-${window.to}.csv`,
      toCsv(
        rows.map(({ s, site }) => ({
          id: s.asset.id,
          name: s.asset.name,
          lon: s.asset.lon,
          lat: s.asset.lat,
          value: s.asset.value ?? "",
          monitored_site: site?.id ?? "",
          findings_within_radius: s.count,
          confirmed: s.confirmed,
          worst_severity: s.worst ?? "",
          nearest_m: s.nearestM === null ? "" : Math.round(s.nearestM),
          observed_change_score: s.score,
          reasons: s.reasons.join("; "),
          radius_m: radius,
          period_from: window.from,
          period_to: window.to,
        })),
      ),
    );

  return (
    <div className="space-y-4">
      <div className="grid gap-4 xl:grid-cols-[380px_1fr]">
        <Card title="Portfolio">
          <div className="space-y-3">
            <textarea className="input h-40 font-mono text-[11px] leading-snug" value={csv} onChange={(e) => setCsv(e.target.value)} spellCheck={false} />
            <div className="flex gap-2">
              <label className="btn flex-1 cursor-pointer justify-center">
                <FileUp className="h-4 w-4" /> Load CSV
                <input
                  type="file"
                  accept=".csv,text/csv,text/plain"
                  className="hidden"
                  onChange={async (e) => {
                    const f = e.target.files?.[0];
                    if (f) setCsv(await f.text());
                  }}
                />
              </label>
              <button className="btn" onClick={() => setCsv(SAMPLE_PORTFOLIO)}>Sample</button>
              <button className="btn" onClick={exportRows} disabled={!rows.length}>
                <Download className="h-4 w-4" /> Export
              </button>
            </div>
            <p className="text-[11px] text-dim">
              Columns are matched by name: <span className="mono">lon/lng/longitude</span>, <span className="mono">lat/latitude</span>, optional{" "}
              <span className="mono">id</span>, <span className="mono">name</span>, <span className="mono">value/tiv</span>. Other columns are kept. The file never leaves
              this browser.
            </p>
            <label className="block">
              <span className="label">Screening radius: {radius} m</span>
              <input type="range" min={50} max={3000} step={50} value={radius} onChange={(e) => setRadius(Number(e.target.value))} className="w-full" />
            </label>
            {parsed.errors.map((e) => <p key={e} className="text-xs text-crit">{e}</p>)}
          </div>
        </Card>
        <Card bodyClass="p-2">
          <WorldMap
            height={420}
            labels={false}
            selected={selected}
            onSelect={setSelected}
            polygons={boundaries.map((b) => ({ id: b.id, ring: b.ring, tone: "site" as const, label: b.name }))}
            points={rows.map(({ s, site }) => ({
              id: s.asset.id,
              lon: s.asset.lon,
              lat: s.asset.lat,
              tone: s.worst ?? (site ? "site" : "object"),
              pulse: s.score >= 70,
              size: 6,
              label: `${s.asset.name} · score ${s.score}`,
            })) as MapPoint[]}
          />
        </Card>
      </div>

      {all.isLoading ? (
        <Loading label="Screening" />
      ) : all.error ? (
        <ErrorState error={all.error} />
      ) : !rows.length ? (
        <Empty title="No assets loaded">Paste a CSV, load a file, or use the sample portfolio.</Empty>
      ) : (
        <>
          <TruncationNotice truncated={all.data?.truncated} limit={all.data?.limit} />
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="Assets screened" value={rows.length} sub={parsed.errors.length ? `${parsed.errors.length} row(s) could not be placed` : "all rows placed"} />
            <Stat label="Inside monitored sites" value={`${covered.length} / ${rows.length}`} sub="the rest are unobserved" tone={covered.length < rows.length ? "med" : "accent"} />
            <Stat label="With nearby findings" value={withFindings.length} sub={`within ${radius} m`} tone="high" />
            <Stat
              label="Value near high/critical"
              value={totalValue ? fmtPct(exposedValue / totalValue) : "—"}
              sub={totalValue ? `${fmtNum(exposedValue)} of ${fmtNum(totalValue)}` : "no value column"}
              tone="crit"
            />
          </div>
          <Card title="Ranked by observed change" bodyClass="p-0">
            <div className="max-h-[520px] overflow-auto">
              <table className="tbl">
                <thead>
                  <tr>
                    <th className="text-right">Score</th>
                    <th>Asset</th>
                    <th className="text-right">Value</th>
                    <th>Monitored site</th>
                    <th className="text-right">Findings</th>
                    <th>Worst</th>
                    <th className="text-right">Nearest</th>
                    <th>Why</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map(({ s, site }) => (
                    <tr key={s.asset.id} className={`row ${selected === s.asset.id ? "bg-raised" : ""}`} onClick={() => setSelected(s.asset.id)}>
                      <td className="text-right">
                        <span className={`font-mono font-semibold ${TONE_TEXT[scoreTone(s.score)]}`}>{s.score}</span>
                      </td>
                      <td>
                        <div className="font-medium">{s.asset.name}</div>
                        <div className="mono text-dim">{s.asset.id}</div>
                      </td>
                      <td className="text-right tabular-nums">{s.asset.value === undefined ? "—" : fmtNum(s.asset.value)}</td>
                      <td>{site ? <Link className="text-accent hover:underline" to={`/sites/${site.id}`}>{site.id}</Link> : <span className="text-med">not monitored</span>}</td>
                      <td className="text-right">{s.count}</td>
                      <td>{s.worst ? <SeverityBadge severity={s.worst} /> : <span className="text-dim">—</span>}</td>
                      <td className="whitespace-nowrap text-right">{s.nearestM === null ? "—" : s.nearestM === 0 ? "on site" : `${fmtNum(s.nearestM)} m`}</td>
                      <td className="max-w-sm text-xs text-dim">{s.reasons.join("; ") || (site ? "no findings in radius" : "outside monitoring — unobserved")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
          {sel && sel.s.nearby.length > 0 && (
            <Card title={`${sel.s.asset.name} — nearby findings`} bodyClass="p-0">
              <ul className="divide-y divide-line">
                {sel.s.nearby.slice(0, 12).map(({ change: c, distanceM: d }) => (
                  <li key={c.id}>
                    <Link className="flex items-center gap-3 px-4 py-2 hover:bg-raised/50" to={`/changes?focus=${c.id}`}>
                      <SeverityBadge severity={c.severity} />
                      <span className="text-sm">{humanise(c.type)} · {fmtArea(c.area_m2)}</span>
                      <span className="ml-auto text-xs text-dim">{d === 0 ? "on site" : `${fmtNum(d)} m`} · {fmtDate(c.detected_on)}</span>
                    </Link>
                  </li>
                ))}
              </ul>
            </Card>
          )}
          <Caveat>
            {SCORE_METHOD} Assets outside every monitored site have no findings because nothing was observed there — enroll the site to screen it.
          </Caveat>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 3. Event footprint
// ---------------------------------------------------------------------------

function EventFootprint({ boundaries, csv }: { boundaries: Boundary[]; csv: string }) {
  const { window } = usePeriod();
  const all = useAllChanges();
  const [ring, setRing] = useState<LonLat[]>([]);
  const [damageOnly, setDamageOnly] = useState(true);
  const assets: Asset[] = useMemo(() => parseAssetsCsv(csv).assets, [csv]);
  const closed = ring.length >= 3;

  const inside = useMemo(() => {
    if (!closed || !all.data) return [];
    return all.data.changes.filter(
      (c) => (!damageOnly || DAMAGE_TYPES.has(c.type)) && (pointInPolygon(centroid(c.geometry), ring) || c.geometry.some((q) => pointInPolygon(q, ring))),
    );
  }, [closed, all.data, ring, damageOnly]);
  const affected = closed ? assets.filter((a) => pointInPolygon([a.lon, a.lat], ring)) : [];
  const sitesHit = closed ? boundaries.filter((b) => b.ring.some((q) => pointInPolygon(q, ring)) || pointInPolygon(centroid(b.ring), ring)) : [];
  const area = inside.reduce((t, c) => t + c.area_m2, 0);
  const byType = Object.entries(inside.reduce<Record<string, number>>((m, c) => ((m[c.type] = (m[c.type] ?? 0) + 1), m), {}));

  const polygons: MapPolygon[] = [
    ...boundaries.map((b) => ({ id: b.id, ring: b.ring, tone: "site" as const, label: b.name })),
    ...(closed ? [{ id: "footprint", ring, tone: "draft" as const, label: "Event footprint" }] : []),
    ...inside.map((c) => ({ id: c.id, ring: c.geometry, tone: c.severity, label: humanise(c.type) })),
  ];

  return (
    <div className="space-y-4">
      <div className="grid gap-4 xl:grid-cols-[340px_1fr]">
        <Card title="Event footprint">
          <div className="space-y-3 text-sm">
            <p className="text-xs text-dim">
              Click the map to outline the area an event touched — a cyclone track, a flood extent, a fire perimeter. Findings and portfolio assets
              inside it are listed for the analysis window ({fmtDate(window.from)} – {fmtDate(window.to)}).
            </p>
            <div className="flex gap-2">
              <button className="btn" onClick={() => setRing((r) => r.slice(0, -1))} disabled={!ring.length}>
                <Undo2 className="h-4 w-4" /> Undo
              </button>
              <button className="btn" onClick={() => setRing([])} disabled={!ring.length}>Clear</button>
            </div>
            <div className="text-xs text-dim">{ring.length} vertex(es){closed ? "" : " — three or more close the footprint"}</div>
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={damageOnly} onChange={(e) => setDamageOnly(e.target.checked)} />
              Damage and water findings only
            </label>
            <p className="text-[11px] text-dim">Counts: possible damage, inundation, structure removed, surface change. Untick to include every finding type.</p>
            <p className="text-[11px] text-dim">Portfolio assets come from the Portfolio tab ({assets.length} loaded).</p>
          </div>
        </Card>
        <Card bodyClass="p-2">
          <WorldMap
            height={440}
            labels={false}
            drawing
            onDrawPoint={(q) => setRing((r) => [...r, q])}
            polygons={polygons}
            points={[
              ...ring.map((q, i) => ({ id: `v${i}`, lon: q[0], lat: q[1], tone: "sat" as const, size: 3.5 })),
              ...assets.map((a) => ({ id: a.id, lon: a.lon, lat: a.lat, tone: (affected.includes(a) ? "critical" : "object") as MapPoint["tone"], size: 5, label: a.name })),
            ]}
          />
        </Card>
      </div>
      {!closed ? (
        <Empty title="Outline the event on the map">Zoom to the area first; each click adds a vertex.</Empty>
      ) : all.isLoading ? (
        <Loading />
      ) : (
        <>
          <TruncationNotice truncated={all.data?.truncated} limit={all.data?.limit} />
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="Findings inside" value={inside.length} sub={byType.map(([k, v]) => `${v} ${humanise(k).toLowerCase()}`).join(", ") || "none"} tone="high" />
            <Stat label="Area changed" value={fmtArea(area)} />
            <Stat label="Monitored sites touched" value={sitesHit.length} sub={sitesHit.map((s) => s.id).join(", ") || "none — footprint is unobserved"} tone={sitesHit.length ? "accent" : "med"} />
            <Stat label="Portfolio assets inside" value={affected.length} sub={affected.length ? fmtNum(affected.reduce((t, a) => t + (a.value ?? 0), 0)) + " total value" : "—"} tone="crit" />
          </div>
          <div className="grid gap-4 xl:grid-cols-2">
            <Card title="Findings in footprint" bodyClass="p-0">
              {inside.length ? (
                <ul className="max-h-[360px] divide-y divide-line overflow-y-auto">
                  {inside.map((c) => (
                    <li key={c.id}>
                      <Link className="flex items-center gap-3 px-4 py-2 hover:bg-raised/50" to={`/changes?focus=${c.id}`}>
                        <SeverityBadge severity={c.severity} />
                        <span className="text-sm">{humanise(c.type)} · {fmtArea(c.area_m2)}</span>
                        <span className="ml-auto text-xs text-dim">{c.aoi_id} · {fmtDate(c.detected_on)}</span>
                      </Link>
                    </li>
                  ))}
                </ul>
              ) : (
                <Empty title="No findings inside this footprint">
                  {sitesHit.length ? "The monitored area inside it showed no matching change in this window." : "No monitored site lies inside it, so nothing was observed."}
                </Empty>
              )}
            </Card>
            <Card title="Portfolio assets in footprint" bodyClass="p-0">
              {affected.length ? (
                <table className="tbl">
                  <thead>
                    <tr>
                      <th>Asset</th>
                      <th className="text-right">Value</th>
                      <th>Monitored</th>
                    </tr>
                  </thead>
                  <tbody>
                    {affected.map((a) => (
                      <tr key={a.id}>
                        <td>{a.name} <span className="mono text-dim">{a.id}</span></td>
                        <td className="text-right">{a.value === undefined ? "—" : fmtNum(a.value)}</td>
                        <td>{coveringSite([a.lon, a.lat], boundaries)?.id ?? <span className="text-med">no</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <Empty title="No portfolio assets inside" />
              )}
            </Card>
          </div>
          <Caveat>
            A finding inside the footprint is change observed there in this window, not damage attributed to the event. Compare dates against the
            event's timing, and open the evidence before reporting a loss.
          </Caveat>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------

type T = "location" | "portfolio" | "event";

export default function AssetIntel() {
  const sites = useSites();
  const boundaries = useBoundaries(sites.data);
  const [tab, setTab] = useState<T>("location");
  // Shared so a portfolio loaded once is used by the event-footprint tab too.
  const [csv, setCsv] = useState(SAMPLE_PORTFOLIO);

  return (
    <div className="space-y-4">
      <PageHeader
        title="Asset intelligence"
        subtitle="The monitoring record from the point of view of a place you own, insure or are responsible for."
        actions={
          <span className="flex items-center gap-1 text-xs text-dim">
            <MapPinned className="h-4 w-4" /> <PenLine className="h-4 w-4" /> computed in the browser from TerraShield findings
          </span>
        }
      />
      <Tabs<T>
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "location", label: "Location report" },
          { id: "portfolio", label: "Portfolio screening" },
          { id: "event", label: "Event footprint" },
        ]}
      />
      {tab === "location" && <LocationReport boundaries={boundaries} />}
      {tab === "portfolio" && <PortfolioScreening boundaries={boundaries} csv={csv} setCsv={setCsv} />}
      {tab === "event" && <EventFootprint boundaries={boundaries} csv={csv} />}
    </div>
  );
}
