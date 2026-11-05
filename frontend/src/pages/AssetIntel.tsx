import { useQueries, useQuery } from "@tanstack/react-query";
import { AlertTriangle, Download, FileUp, Layers, Printer, Search, Trash2, Undo2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useSites } from "../api/hooks";
import type { Change, FieldsResponse, LonLat, Scene, SiteDetail, SiteSummary } from "../api/types";
import { WorldMap, type MapLine, type MapPoint, type MapPolygon } from "../components/WorldMap";
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
import { searchPlaces, type Place } from "../lib/gazetteer";
import { assess, HAZARD_LABEL, layerFromGeoJson, syntheticDemoLayers, type HazardKind, type HazardLayer, type HazardResult, type LayerMode } from "../lib/hazards";
import { ACTION_TONE, recommend, STALE_DAYS, type Action, type Recommendation } from "../lib/inspection";
import { usePeriod } from "../state/period";

// Asset intelligence: the monitoring record read from the point of view of a
// place someone owns, insures or is responsible for.
//   - Location report: one place — what changed, which hazards, inspect or not.
//   - Portfolio: many places — ranked, with an inspection queue.
//   - Event footprint: an event's area — which findings and assets are inside.
//   - Hazard layers: the exposure data those answers draw on.

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

/**
 * The last usable acquisition per site, in the analysis window. "No change
 * nearby" is only worth something if the site was actually seen recently.
 */
function useLastLooks(sites: SiteSummary[] | undefined) {
  const { window } = usePeriod();
  const results = useQueries({
    queries: (sites ?? []).map((s) => ({
      queryKey: ["imagery", s.id, window.from, window.to],
      queryFn: () => api<{ site: string; scenes: Scene[] }>("/api/imagery", { query: { site: s.id, from: window.from, to: window.to } }),
    })),
  });
  return useMemo(() => {
    const m = new Map<string, { last: string | null; usable: number; acquired: number }>();
    results.forEach((r) => {
      if (!r.data) return;
      const usable = r.data.scenes.filter((x) => x.usable);
      m.set(r.data.site, {
        last: usable.reduce<string | null>((a, x) => (!a || x.acquired_on > a ? x.acquired_on : a), null),
        usable: usable.length,
        acquired: r.data.scenes.length,
      });
    });
    return m;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [results.map((r) => r.dataUpdatedAt).join(",")]);
}

/** Sites and their named fields, for offline place search. */
function useKnownPlaces(sites: SiteSummary[] | undefined): Place[] {
  const fields = useQueries({
    queries: (sites ?? []).map((s) => ({
      queryKey: ["fields", s.id],
      queryFn: () => api<FieldsResponse>(`/api/sites/${encodeURIComponent(s.id)}/fields`),
      staleTime: 600_000,
    })),
  });
  return useMemo(
    () => [
      ...(sites ?? []).map((s) => ({ label: s.name, kind: "site" as const, lonlat: s.centroid, ref: s.id })),
      ...fields.flatMap((f) =>
        (f.data?.fields ?? []).filter((x) => x.active).map((x) => ({ label: `${x.name} (${x.aoi_id})`, kind: "field" as const, lonlat: x.centroid, ref: x.id })),
      ),
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [sites, fields.map((f) => f.dataUpdatedAt).join(",")],
  );
}

type Boundary = { id: string; name: string; ring: LonLat[] };

function coveringSite(p: LonLat, boundaries: Boundary[]): Boundary | undefined {
  return boundaries.find((b) => b.ring.length >= 3 && pointInPolygon(p, b.ring));
}

function daysBetween(a: string, b: string): number {
  return Math.round((Date.parse(`${b}T00:00:00Z`) - Date.parse(`${a}T00:00:00Z`)) / 86_400_000);
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

function SyntheticNotice({ layers }: { layers: HazardLayer[] }) {
  if (!layers.some((l) => l.synthetic)) return null;
  return (
    <div className="flex gap-2 rounded-md border border-med/40 bg-med/5 p-3 text-sm text-med">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
      Synthetic demo hazard layers are loaded. Their zones and faults are invented to show the workflow and say nothing about real exposure. Replace
      them with your flood, fault and landslide data on the Hazard layers tab.
    </div>
  );
}

function ActionBadge({ action }: { action: Action }) {
  return <span className={`chip ${ACTION_TONE[action]}`}>{action}</span>;
}

/** Draw hazard layers on a map: zones as tinted polygons, lines dashed. */
function hazardGeometry(layers: HazardLayer[]): { polygons: MapPolygon[]; lines: MapLine[] } {
  const polygons: MapPolygon[] = [];
  const lines: MapLine[] = [];
  layers.forEach((l) => {
    l.polygons.forEach((p, i) => polygons.push({ id: `${l.id}-p${i}`, ring: p.rings[0], tone: "field", label: `${l.name}: ${p.cls}` }));
    l.lines.forEach((ln, i) => lines.push({ id: `${l.id}-l${i}`, coords: ln, tone: "accent" }));
  });
  return { polygons, lines };
}

/** Everything known about one asset, assembled once and shared by both views. */
interface AssetView {
  s: Screening;
  site?: Boundary;
  hazards: HazardResult[];
  daysSinceLook: number | null;
  lastLook: string | null;
  rec: Recommendation;
}

function useAssetViews(assets: Asset[], radius: number, layers: HazardLayer[], boundaries: Boundary[], sites: SiteSummary[] | undefined) {
  const { window } = usePeriod();
  const all = useAllChanges();
  const looks = useLastLooks(sites);
  const values = assets.map((a) => a.value).filter((v): v is number => v !== undefined).sort((a, b) => a - b);
  const median = values.length ? values[Math.floor(values.length / 2)] : undefined;
  const views = useMemo<AssetView[]>(() => {
    if (!all.data) return [];
    return assets.map((a) => {
      const p: LonLat = [a.lon, a.lat];
      const s = screenAsset(a, all.data.changes, radius);
      const site = coveringSite(p, boundaries);
      const hazards = layers.map((l) => assess(p, l));
      const lastLook = site ? looks.get(site.id)?.last ?? null : null;
      const daysSinceLook = lastLook ? daysBetween(lastLook, window.to) : null;
      const rec = recommend({
        screening: s,
        hazards,
        monitored: !!site,
        daysSinceLook,
        valueRatio: a.value !== undefined && median ? a.value / median : undefined,
      });
      return { s, site, hazards, daysSinceLook, lastLook, rec };
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [assets, all.data, radius, layers, boundaries, looks, window.to, median]);
  return { views, all };
}

// ---------------------------------------------------------------------------
// The living record: one asset's findings, past to present
// ---------------------------------------------------------------------------

function LivingRecord({ view }: { view: AssetView }) {
  const byMonth = useMemo(() => {
    const m = new Map<string, { change: Change; distanceM: number }[]>();
    [...view.s.nearby]
      .sort((a, b) => b.change.detected_on.localeCompare(a.change.detected_on))
      .forEach((x) => {
        const k = x.change.detected_on.slice(0, 7);
        m.set(k, [...(m.get(k) ?? []), x]);
      });
    return [...m.entries()];
  }, [view.s.nearby]);
  if (!byMonth.length)
    return <p className="text-sm text-dim">{view.site ? "No findings near this asset in the analysis window." : "Nothing observed: the asset is outside every monitored site."}</p>;
  return (
    <ol className="relative space-y-4 border-l border-line pl-5">
      {byMonth.map(([month, items]) => (
        <li key={month}>
          <span className="absolute -left-[5px] mt-1.5 h-2.5 w-2.5 rounded-full bg-accent" />
          <div className="text-xs font-semibold uppercase tracking-wider text-dim">
            {new Date(`${month}-01T00:00:00Z`).toLocaleDateString("en-GB", { month: "long", year: "numeric", timeZone: "UTC" })} · {items.length} finding(s)
          </div>
          <ul className="mt-1 space-y-1">
            {items.slice(0, 6).map(({ change: c, distanceM: d }) => (
              <li key={c.id} className="flex flex-wrap items-center gap-2 text-sm">
                <SeverityBadge severity={c.severity} />
                <Link className="hover:underline" to={`/changes?focus=${c.id}`}>{humanise(c.type)}</Link>
                <span className="text-xs text-dim">
                  {fmtDate(c.detected_on)} · {fmtArea(c.area_m2)} · {d === 0 ? "on the asset" : `${fmtNum(d)} m`}
                </span>
                <ReviewBadge status={c.review_status} />
              </li>
            ))}
            {items.length > 6 && <li className="text-xs text-dim">+{items.length - 6} more this month</li>}
          </ul>
        </li>
      ))}
    </ol>
  );
}

function HazardTable({ hazards }: { hazards: HazardResult[] }) {
  if (!hazards.length) return <p className="text-sm text-dim">No hazard layers loaded. Add flood, fault or landslide data on the Hazard layers tab.</p>;
  return (
    <table className="tbl">
      <thead>
        <tr>
          <th>Hazard</th>
          <th>Layer</th>
          <th>Result</th>
        </tr>
      </thead>
      <tbody>
        {hazards.map((h) => (
          <tr key={h.layer.id}>
            <td>{HAZARD_LABEL[h.layer.kind]}</td>
            <td className="text-xs">
              {h.layer.name}
              <div className="text-dim">{h.layer.source}</div>
            </td>
            <td className={h.flagged ? "font-medium text-high" : "text-dim"}>
              {h.layer.mode === "distance" ? `${h.value} to nearest line` : h.value}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ---------------------------------------------------------------------------
// 1. Location report
// ---------------------------------------------------------------------------

function LocationReport({ boundaries, layers, sites }: { boundaries: Boundary[]; layers: HazardLayer[]; sites: SiteSummary[] | undefined }) {
  const { window } = usePeriod();
  const known = useKnownPlaces(sites);
  const [p, setP] = useState<LonLat>([69.7028, 22.743]);
  const [label, setLabel] = useState("Mundra terminal warehouse");
  const [query, setQuery] = useState("");
  const [radius, setRadius] = useState(500);
  const matches = useMemo(() => searchPlaces(query, known), [query, known]);
  const asset = useMemo<Asset[]>(() => [{ id: "point", name: label, lon: p[0], lat: p[1] }], [p, label]);
  const { views, all } = useAssetViews(asset, radius, layers, boundaries, sites);
  const view = views[0];
  const site = view?.site;
  const field = useQuery({
    queryKey: ["field-at", p[0], p[1], site?.id],
    queryFn: () => api<{ field: { name: string; use: string } | null }>("/api/fields/at", { query: { lon: p[0], lat: p[1], aoi_id: site?.id } }),
    enabled: !!site,
  });

  const circle = circleRing(p, radius);
  const focus: [[number, number], [number, number]] = [
    [Math.min(...circle.map((q) => q[0])), Math.min(...circle.map((q) => q[1]))],
    [Math.max(...circle.map((q) => q[0])), Math.max(...circle.map((q) => q[1]))],
  ];
  const hz = hazardGeometry(layers);

  const pick = (pl: Place) => {
    setP(pl.lonlat);
    setLabel(pl.kind === "coordinate" ? "Selected location" : pl.label);
    setQuery("");
  };

  return (
    <div className="space-y-4">
      <SyntheticNotice layers={layers} />
      <div className="grid gap-4 xl:grid-cols-[340px_1fr]">
        <Card title="Location" className="print:hidden">
          <div className="space-y-3">
            <div className="relative">
              <span className="label">Search a site, field or coordinate</span>
              <div className="relative">
                <Search className="pointer-events-none absolute left-2.5 top-2 h-4 w-4 text-dim" />
                <input
                  className="input pl-8"
                  placeholder={`22.743, 69.703 · 22°44'35"N 69°42'10"E · Mundra`}
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && matches[0] && pick(matches[0])}
                />
              </div>
              {query && (
                <ul className="card absolute z-20 mt-1 w-full overflow-hidden shadow-xl">
                  {matches.map((m) => (
                    <li key={`${m.kind}-${m.label}`}>
                      <button className="flex w-full justify-between gap-2 px-3 py-2 text-left text-sm hover:bg-raised" onClick={() => pick(m)}>
                        <span className="truncate">{m.label}</span>
                        <span className="text-xs text-dim">{m.kind}</span>
                      </button>
                    </li>
                  ))}
                  {!matches.length && (
                    <li className="px-3 py-2 text-xs text-dim">
                      No match. Street addresses need a geocoder, which this offline console does not call — paste coordinates instead.
                    </li>
                  )}
                </ul>
              )}
            </div>
            <div className="rounded-md bg-raised/60 px-3 py-2 text-sm">
              <div className="font-medium">{label}</div>
              <div className="font-mono text-xs text-dim">{fmtLonLat(p[0], p[1])}</div>
            </div>
            <label className="block">
              <span className="label">Search radius: {radius} m</span>
              <input type="range" min={50} max={3000} step={50} value={radius} onChange={(e) => setRadius(Number(e.target.value))} className="w-full" />
            </label>
            <p className="text-xs text-dim">Or click the map. The report uses the analysis window in the top bar.</p>
            <button className="btn w-full justify-center" onClick={() => globalThis.print()} disabled={!view}>
              <Printer className="h-4 w-4" /> Print / save as PDF
            </button>
          </div>
        </Card>
        <Card bodyClass="p-2">
          <WorldMap
            height={400}
            focus={focus}
            drawing
            onDrawPoint={(q) => (setP(q), setLabel("Selected location"))}
            lines={hz.lines}
            polygons={[
              ...hz.polygons,
              ...(site ? [{ id: site.id, ring: site.ring, tone: "site" as const, label: site.name }] : []),
              { id: "radius", ring: circle, tone: "draft" as const, label: `${radius} m search radius` },
              ...(view?.s.nearby.map(({ change: c }) => ({ id: c.id, ring: c.geometry, tone: c.severity, label: `${humanise(c.type)} · ${fmtDate(c.detected_on)}` })) ?? []),
            ]}
            points={[{ id: "pt", lon: p[0], lat: p[1], tone: "sat", size: 6, pulse: true, label }]}
          />
        </Card>
      </div>

      {all.isLoading || !view ? (
        all.error ? <ErrorState error={all.error} /> : <Loading label="Reading the record" />
      ) : (
        <div className="space-y-4">
          <TruncationNotice truncated={all.data?.truncated} limit={all.data?.limit} />
          <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
            <div className="card px-4 py-3">
              <div className="card-t">Recommendation</div>
              <div className="mt-2">
                <ActionBadge action={view.rec.action} />
              </div>
              <div className="mt-1 text-xs text-dim">{view.rec.reasons.length} reason(s) below</div>
            </div>
            <Stat label="Observed-change score" value={view.s.score} sub="0–100, see method" tone={scoreTone(view.s.score)} />
            <Stat label="Findings in radius" value={view.s.count} sub={`${view.s.confirmed} confirmed`} tone="high" />
            <Stat label="Hazard flags" value={view.hazards.filter((h) => h.flagged).length} sub={`of ${view.hazards.length} layer(s)`} tone={view.hazards.some((h) => h.flagged) ? "high" : "ink"} />
            <Stat
              label="Last usable image"
              value={view.lastLook ? `${view.daysSinceLook} d` : "—"}
              sub={view.lastLook ? `${fmtDate(view.lastLook)}, before window end` : site ? "none in window" : "site not monitored"}
              tone={view.daysSinceLook !== null && view.daysSinceLook > STALE_DAYS ? "med" : "ink"}
            />
          </div>
          <div className="grid gap-4 xl:grid-cols-2">
            <Card title="Asset report">
              <KeyValue
                rows={[
                  ["Asset", label],
                  ["Location", <span key="l" className="font-mono">{fmtLonLat(p[0], p[1])}</span>],
                  ["Period", `${fmtDate(window.from)} – ${fmtDate(window.to)}`],
                  [
                    "Monitoring",
                    site ? (
                      <Link key="s" className="text-accent hover:underline" to={`/sites/${site.id}`}>{site.name}</Link>
                    ) : (
                      <span key="s" className="text-med">Not inside any monitored site — absence of findings means unobserved, not unchanged.</span>
                    ),
                  ],
                  ...(site ? ([["Field", field.data?.field ? `${field.data.field.name}${field.data.field.use ? ` (${field.data.field.use})` : ""}` : "No named field at this point"]] as [string, string][]) : []),
                  ["Why", <ul key="w" className="list-disc space-y-0.5 pl-4">{view.rec.reasons.map((r) => <li key={r}>{r}</li>)}</ul>],
                ]}
              />
            </Card>
            <Card title="Hazard exposure" bodyClass="p-0">
              <HazardTable hazards={view.hazards} />
            </Card>
          </div>
          <Card title="Living record — findings near this asset, newest first">
            <LivingRecord view={view} />
          </Card>
          <Caveat>
            {SCORE_METHOD} The recommendation counts one point per hazard flag, two for a high or critical change, one each for an analyst
            confirmation, a change on the asset itself, a last image older than {STALE_DAYS} days, and a value twice the portfolio median: three or more
            is Inspect, one or two is Desk review.
          </Caveat>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 2. Portfolio screening and the inspection queue
// ---------------------------------------------------------------------------

function download(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

const ACTION_ORDER: Record<Action, number> = { Inspect: 0, "Not observed": 1, "Desk review": 2, "No action": 3 };

function PortfolioScreening({
  boundaries,
  csv,
  setCsv,
  layers,
  sites,
}: {
  boundaries: Boundary[];
  csv: string;
  setCsv: (s: string) => void;
  layers: HazardLayer[];
  sites: SiteSummary[] | undefined;
}) {
  const { window } = usePeriod();
  const [radius, setRadius] = useState(500);
  const [selected, setSelected] = useState<string | null>(null);
  const [only, setOnly] = useState<Action | "">("");
  const parsed = useMemo(() => parseAssetsCsv(csv), [csv]);
  const { views, all } = useAssetViews(parsed.assets, radius, layers, boundaries, sites);

  const ranked = useMemo(
    () =>
      [...views].sort(
        (x, y) =>
          ACTION_ORDER[x.rec.action] - ACTION_ORDER[y.rec.action] ||
          y.rec.priority - x.rec.priority ||
          (y.s.asset.value ?? 0) - (x.s.asset.value ?? 0),
      ),
    [views],
  );
  const rows = only ? ranked.filter((v) => v.rec.action === only) : ranked;
  const count = (a: Action) => views.filter((v) => v.rec.action === a).length;
  const totalValue = views.reduce((t, v) => t + (v.s.asset.value ?? 0), 0);
  const inspectValue = views.filter((v) => v.rec.action === "Inspect").reduce((t, v) => t + (v.s.asset.value ?? 0), 0);
  const sel = views.find((v) => v.s.asset.id === selected);
  const hz = hazardGeometry(layers);

  const exportRows = () =>
    download(
      `terrashield-portfolio-${window.from}-${window.to}.csv`,
      toCsv(
        ranked.map((v) => ({
          id: v.s.asset.id,
          name: v.s.asset.name,
          lon: v.s.asset.lon,
          lat: v.s.asset.lat,
          value: v.s.asset.value ?? "",
          recommendation: v.rec.action,
          reasons: v.rec.reasons.join("; "),
          monitored_site: v.site?.id ?? "",
          last_usable_image: v.lastLook ?? "",
          findings_within_radius: v.s.count,
          worst_severity: v.s.worst ?? "",
          observed_change_score: v.s.score,
          ...Object.fromEntries(v.hazards.map((h) => [`hazard_${h.layer.kind}_${h.layer.id}`, h.value])),
          synthetic_hazard_layers: layers.some((l) => l.synthetic) ? "yes" : "no",
          radius_m: radius,
          period_from: window.from,
          period_to: window.to,
        })),
      ),
    );

  return (
    <div className="space-y-4">
      <SyntheticNotice layers={layers} />
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
              <button className="btn" onClick={exportRows} disabled={!views.length}>
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
            lines={hz.lines}
            polygons={[...hz.polygons, ...boundaries.map((b) => ({ id: b.id, ring: b.ring, tone: "site" as const, label: b.name }))]}
            points={views.map((v) => ({
              id: v.s.asset.id,
              lon: v.s.asset.lon,
              lat: v.s.asset.lat,
              tone: v.rec.action === "Inspect" ? "critical" : v.rec.action === "Desk review" ? "medium" : v.rec.action === "Not observed" ? "object" : "site",
              pulse: v.rec.action === "Inspect",
              size: 6,
              label: `${v.s.asset.name} · ${v.rec.action}`,
            })) as MapPoint[]}
          />
        </Card>
      </div>

      {all.isLoading ? (
        <Loading label="Screening" />
      ) : all.error ? (
        <ErrorState error={all.error} />
      ) : !views.length ? (
        <Empty title="No assets loaded">Paste a CSV, load a file, or use the sample portfolio.</Empty>
      ) : (
        <>
          <TruncationNotice truncated={all.data?.truncated} limit={all.data?.limit} />
          <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
            <Stat label="Assets screened" value={views.length} sub={parsed.errors.length ? `${parsed.errors.length} row(s) could not be placed` : "all rows placed"} />
            <Stat label="Inspect" value={count("Inspect")} sub={totalValue ? `${fmtPct(inspectValue / totalValue)} of portfolio value` : "physical visit suggested"} tone="crit" />
            <Stat label="Desk review" value={count("Desk review")} tone="med" />
            <Stat label="No action" value={count("No action")} tone="accent" />
            <Stat label="Not observed" value={count("Not observed")} sub="outside every monitored site" tone="info" />
          </div>
          <Card
            title="Inspection queue"
            bodyClass="p-0"
            actions={
              <select className="input w-auto py-1 text-xs" value={only} onChange={(e) => setOnly(e.target.value as Action | "")} aria-label="Filter by recommendation">
                <option value="">All recommendations</option>
                {(["Inspect", "Desk review", "No action", "Not observed"] as Action[]).map((a) => (
                  <option key={a} value={a}>{a}</option>
                ))}
              </select>
            }
          >
            <div className="max-h-[520px] overflow-auto">
              <table className="tbl">
                <thead>
                  <tr>
                    <th>Recommendation</th>
                    <th>Asset</th>
                    <th className="text-right">Value</th>
                    <th className="text-right">Score</th>
                    <th>Hazards</th>
                    <th>Last image</th>
                    <th>Why</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((v) => (
                    <tr key={v.s.asset.id} className={`row ${selected === v.s.asset.id ? "bg-raised" : ""}`} onClick={() => setSelected(v.s.asset.id)}>
                      <td><ActionBadge action={v.rec.action} /></td>
                      <td>
                        <div className="font-medium">{v.s.asset.name}</div>
                        <div className="mono text-dim">{v.s.asset.id} · {v.site ? v.site.id : "not monitored"}</div>
                      </td>
                      <td className="text-right tabular-nums">{v.s.asset.value === undefined ? "—" : fmtNum(v.s.asset.value)}</td>
                      <td className="text-right">
                        <span className={`font-mono font-semibold ${TONE_TEXT[scoreTone(v.s.score)]}`}>{v.s.score}</span>
                      </td>
                      <td className="text-xs">
                        {v.hazards.filter((h) => h.flagged).map((h) => HAZARD_LABEL[h.layer.kind]).join(", ") || <span className="text-dim">none flagged</span>}
                      </td>
                      <td className="whitespace-nowrap text-xs">{v.lastLook ? `${v.daysSinceLook} d ago` : "—"}</td>
                      <td className="max-w-sm text-xs text-dim">{v.rec.reasons.join("; ")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
          {sel && (
            <div className="grid gap-4 xl:grid-cols-[1.2fr_1fr]">
              <Card title={`${sel.s.asset.name} — living record`}>
                <LivingRecord view={sel} />
              </Card>
              <Card title="Hazard exposure" bodyClass="p-0">
                <HazardTable hazards={sel.hazards} />
              </Card>
            </div>
          )}
          <Caveat>
            {SCORE_METHOD} Recommendations order the queue for people; they do not price, decline or settle anything. Assets outside every monitored
            site are “Not observed” — enroll the site to screen them.
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
// 4. Hazard layers
// ---------------------------------------------------------------------------

const KINDS: HazardKind[] = ["flood", "wildfire", "landslide", "earthquake", "other"];

function HazardLayers({ layers, setLayers }: { layers: HazardLayer[]; setLayers: (l: HazardLayer[]) => void }) {
  const [name, setName] = useState("");
  const [kind, setKind] = useState<HazardKind>("flood");
  const [mode, setMode] = useState<LayerMode>("zone");
  const [classField, setClassField] = useState("zone");
  const [source, setSource] = useState("");
  const [text, setText] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const hz = hazardGeometry(layers);

  const add = () => {
    const r = layerFromGeoJson(text, { name: name || `${HAZARD_LABEL[kind]} layer`, kind, mode, classField, source: source || "Not stated" });
    if (r.error || !r.layer) return setMsg({ ok: false, text: r.error ?? "Could not read the layer." });
    setLayers([...layers, r.layer]);
    setMsg({
      ok: true,
      text: `Added ${r.layer.polygons.length} polygon(s) and ${r.layer.lines.length} line(s)${r.skipped ? `; ${r.skipped} feature(s) of other geometry types skipped` : ""}.`,
    });
    setText("");
    setName("");
  };

  return (
    <div className="space-y-4">
      <SyntheticNotice layers={layers} />
      <div className="grid gap-4 xl:grid-cols-[1fr_1.2fr]">
        <Card
          title={`Loaded layers (${layers.length})`}
          bodyClass="p-0"
          actions={
            <button className="btn text-xs" onClick={() => setLayers([...layers.filter((l) => !l.synthetic), ...syntheticDemoLayers()])}>
              Load synthetic demo layers
            </button>
          }
        >
          {layers.length ? (
            <ul className="divide-y divide-line">
              {layers.map((l) => (
                <li key={l.id} className="flex items-start gap-3 px-4 py-3">
                  <Layers className="mt-0.5 h-4 w-4 text-info" />
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium">
                      {l.name} {l.synthetic && <span className="chip ml-1 border-med/50 text-med">synthetic</span>}
                    </div>
                    <div className="text-xs text-dim">
                      {HAZARD_LABEL[l.kind]} · {l.mode === "zone" ? `zones by “${l.classField}”, ${l.polygons.length} polygon(s)` : `distance to ${l.lines.length} line(s)`}
                    </div>
                    <div className="text-xs text-dim">Source: {l.source}</div>
                  </div>
                  <button className="btn btn-ghost px-2" onClick={() => setLayers(layers.filter((x) => x.id !== l.id))} aria-label={`Remove ${l.name}`}>
                    <Trash2 className="h-4 w-4" />
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <Empty title="No hazard layers">
              Load your own data below, or the synthetic demo layers to see the workflow. TerraShield ships no hazard data.
            </Empty>
          )}
        </Card>
        <Card bodyClass="p-2">
          <WorldMap height={360} lines={hz.lines} polygons={hz.polygons} focus={null} />
        </Card>
      </div>
      <Card title="Add a layer (GeoJSON)">
        <div className="grid gap-4 lg:grid-cols-[320px_1fr]">
          <div className="space-y-2">
            <input className="input" placeholder="Name, e.g. CWC flood hazard 2024" value={name} onChange={(e) => setName(e.target.value)} />
            <div className="grid grid-cols-2 gap-2">
              <select className="input" value={kind} onChange={(e) => setKind(e.target.value as HazardKind)}>
                {KINDS.map((k) => <option key={k} value={k}>{HAZARD_LABEL[k]}</option>)}
              </select>
              <select className="input" value={mode} onChange={(e) => setMode(e.target.value as LayerMode)}>
                <option value="zone">Zones (polygons)</option>
                <option value="distance">Distance to lines</option>
              </select>
            </div>
            {mode === "zone" && (
              <label className="block">
                <span className="label">Property holding the class</span>
                <input className="input font-mono" value={classField} onChange={(e) => setClassField(e.target.value)} />
              </label>
            )}
            <input className="input" placeholder="Source and date, e.g. NDMA 2023" value={source} onChange={(e) => setSource(e.target.value)} />
            <label className="btn w-full cursor-pointer justify-center">
              <FileUp className="h-4 w-4" /> Load .geojson
              <input
                type="file"
                accept=".geojson,.json,application/geo+json,application/json"
                className="hidden"
                onChange={async (e) => {
                  const f = e.target.files?.[0];
                  if (f) {
                    setText(await f.text());
                    if (!name) setName(f.name.replace(/\.(geo)?json$/i, ""));
                  }
                }}
              />
            </label>
            <button className="btn btn-primary w-full justify-center" disabled={!text.trim()} onClick={add}>
              Add layer
            </button>
            {msg && <p className={`text-xs ${msg.ok ? "text-accent" : "text-crit"}`}>{msg.text}</p>}
          </div>
          <textarea
            className="input h-56 font-mono text-[11px]"
            placeholder='{"type":"FeatureCollection","features":[{"type":"Feature","properties":{"zone":"High"},"geometry":{"type":"Polygon","coordinates":[[[69.69,22.73],[69.72,22.73],[69.72,22.75],[69.69,22.75],[69.69,22.73]]]}}]}'
            value={text}
            onChange={(e) => setText(e.target.value)}
            spellCheck={false}
          />
        </div>
      </Card>
      <Caveat>
        Layers live in this browser only and are not shared with other analysts or sent to the server. Zone classes named none, minimal, very low,
        outside or X are read as unflagged; every other class flags. A distance layer flags within 10 km. Use the hazard authority's own
        classification, and state its source and date: a result is only as current as the map behind it.
      </Caveat>
    </div>
  );
}

// ---------------------------------------------------------------------------

type T = "location" | "portfolio" | "event" | "hazards";

const LAYERS_KEY = "terrashield.hazard-layers";

function loadLayers(): HazardLayer[] {
  try {
    const raw = localStorage.getItem(LAYERS_KEY);
    return raw ? (JSON.parse(raw) as HazardLayer[]) : [];
  } catch {
    return [];
  }
}

export default function AssetIntel() {
  const sites = useSites();
  const boundaries = useBoundaries(sites.data);
  const [tab, setTab] = useState<T>("location");
  // Shared so a portfolio loaded once is used by the event-footprint tab too.
  const [csv, setCsv] = useState(SAMPLE_PORTFOLIO);
  const [layers, setLayers] = useState<HazardLayer[]>(loadLayers);

  useEffect(() => {
    try {
      localStorage.setItem(LAYERS_KEY, JSON.stringify(layers));
    } catch {
      /* too large or storage blocked: layers last for this page only */
    }
  }, [layers]);

  return (
    <div className="space-y-4">
      <PageHeader
        title="Asset intelligence"
        subtitle="Which assets changed, what they are exposed to, and which are worth sending someone to inspect."
        actions={<span className="text-xs text-dim">computed in the browser from TerraShield findings and your hazard layers</span>}
      />
      <Tabs<T>
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "location", label: "Location report" },
          { id: "portfolio", label: "Portfolio & inspections" },
          { id: "event", label: "Event footprint" },
          { id: "hazards", label: `Hazard layers (${layers.length})` },
        ]}
      />
      {tab === "location" && <LocationReport boundaries={boundaries} layers={layers} sites={sites.data} />}
      {tab === "portfolio" && <PortfolioScreening boundaries={boundaries} csv={csv} setCsv={setCsv} layers={layers} sites={sites.data} />}
      {tab === "event" && <EventFootprint boundaries={boundaries} csv={csv} />}
      {tab === "hazards" && <HazardLayers layers={layers} setLayers={setLayers} />}
    </div>
  );
}
