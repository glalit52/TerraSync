import { Layers } from "lucide-react";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAlerts, useChanges, useObjects, useSite, useSites } from "../api/hooks";
import type { Severity } from "../api/types";
import { WorldMap, type MapPoint, type MapPolygon } from "../components/WorldMap";
import { Card, KeyValue, PageHeader, SeverityBadge } from "../components/ui";
import { fmtArea, fmtDate, fmtNum, humanise } from "../lib/format";

export default function MapPage() {
  const nav = useNavigate();
  const sites = useSites();
  const changes = useChanges();
  const alerts = useAlerts({ status: "pending" });
  const [focus, setFocus] = useState<string>("");
  const site = useSite(focus || undefined);
  const objects = useObjects(focus || undefined);
  const [layers, setLayers] = useState({ sites: true, changes: true, alerts: true, objects: false });
  const [minSev, setMinSev] = useState<Severity>("low");
  const [selected, setSelected] = useState<string | null>(null);
  const rank = { low: 1, medium: 2, high: 3, critical: 4 } as const;

  const polygons = useMemo<MapPolygon[]>(() => {
    const out: MapPolygon[] = [];
    if (layers.sites && site.data) out.push({ id: site.data.site.id, ring: site.data.site.boundary, tone: "site", label: site.data.site.name });
    if (layers.changes)
      (changes.data ?? [])
        .filter((c) => rank[c.severity] >= rank[minSev] && (!focus || c.aoi_id === focus))
        .forEach((c) => out.push({ id: c.id, ring: c.geometry, tone: c.severity, label: `${humanise(c.type)} · ${fmtDate(c.detected_on)}` }));
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layers, site.data, changes.data, minSev, focus]);

  const hot = new Set((alerts.data?.alerts ?? []).filter((a) => a.priority <= 2).map((a) => a.aoi_id));
  const points = useMemo<MapPoint[]>(() => {
    const out: MapPoint[] = [];
    if (layers.sites)
      (sites.data ?? []).forEach((s) =>
        out.push({
          id: s.id,
          lon: s.centroid[0],
          lat: s.centroid[1],
          tone: layers.alerts && hot.has(s.id) ? "critical" : "site",
          pulse: layers.alerts && hot.has(s.id),
          label: s.name.split(" — ")[0],
        }),
      );
    if (layers.objects && objects.data) {
      const last = objects.data.reduce((m, x) => (x.observed_at > m ? x.observed_at : m), "");
      objects.data.filter((o) => o.observed_at === last).forEach((o) => out.push({ id: o.id, lon: o.lon, lat: o.lat, tone: "object", label: humanise(o.class) }));
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layers, sites.data, objects.data, alerts.data]);

  const focusBox = site.data
    ? ((): [[number, number], [number, number]] => {
        const xs = site.data.site.boundary.map((p) => p[0]);
        const ys = site.data.site.boundary.map((p) => p[1]);
        return [[Math.min(...xs), Math.min(...ys)], [Math.max(...xs), Math.max(...ys)]];
      })()
    : null;

  const selChange = changes.data?.find((c) => c.id === selected);
  const selSite = sites.data?.find((s) => s.id === selected);

  return (
    <div className="space-y-3">
      <PageHeader title="Global map" subtitle="Sites, changes and detections on an offline basemap. Click a site to zoom in." />
      <div className="grid gap-3 xl:grid-cols-[240px_1fr_300px]">
        <Card title={<span className="flex items-center gap-1.5"><Layers className="h-3.5 w-3.5" /> Layers</span>}>
          <div className="space-y-2 text-sm">
            {(Object.keys(layers) as (keyof typeof layers)[]).map((k) => (
              <label key={k} className="flex items-center gap-2">
                <input type="checkbox" checked={layers[k]} onChange={(e) => setLayers((l) => ({ ...l, [k]: e.target.checked }))} />
                {humanise(k)}
                {k === "objects" && !focus && <span className="text-[10px] text-dim">(zoom to a site)</span>}
              </label>
            ))}
            <label className="block pt-2">
              <span className="label">Minimum change severity</span>
              <select className="input" value={minSev} onChange={(e) => setMinSev(e.target.value as Severity)}>
                {["low", "medium", "high", "critical"].map((s) => <option key={s} value={s}>{humanise(s)}</option>)}
              </select>
            </label>
            <label className="block">
              <span className="label">Zoom to site</span>
              <select className="input" value={focus} onChange={(e) => setFocus(e.target.value)}>
                <option value="">Whole estate</option>
                {sites.data?.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </select>
            </label>
          </div>
        </Card>
        <Card bodyClass="p-1">
          <WorldMap
            height="calc(100vh - 200px)"
            polygons={polygons}
            points={points}
            focus={focusBox}
            selected={selected}
            onSelect={(id) => {
              setSelected(id);
              if (sites.data?.some((s) => s.id === id)) setFocus(id);
            }}
          />
        </Card>
        <Card title="Selection">
          {selChange ? (
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <SeverityBadge severity={selChange.severity} />
                <span className="font-medium">{humanise(selChange.type)}</span>
              </div>
              <p className="text-sm text-dim">{selChange.explanation}</p>
              <KeyValue rows={[["Detected", fmtDate(selChange.detected_on)], ["Area", fmtArea(selChange.area_m2)], ["Site", selChange.aoi_id]]} />
              <button className="btn btn-primary" onClick={() => nav(`/changes?focus=${selChange.id}`)}>Open in change viewer</button>
            </div>
          ) : selSite ? (
            <div className="space-y-3">
              <div className="font-medium">{selSite.name}</div>
              <p className="text-sm text-dim">{selSite.description}</p>
              <KeyValue rows={[["Kind", humanise(selSite.kind)], ["Area", `${fmtNum(selSite.area_km2, 2)} km²`], ["Pending P1/P2", hot.has(selSite.id) ? "yes" : "none"]]} />
              <button className="btn btn-primary" onClick={() => nav(`/sites/${selSite.id}`)}>Open site</button>
            </div>
          ) : (
            <p className="text-sm text-dim">Click a site or a change outline.</p>
          )}
        </Card>
      </div>
    </div>
  );
}
