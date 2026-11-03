import { useMemo, useState } from "react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useObjects, useObservations, useSites } from "../api/hooks";
import { WorldMap } from "../components/WorldMap";
import { Card, Caveat, Empty, PageHeader, Q, Stat } from "../components/ui";
import { fmtDate, fmtNum, fmtPct } from "../lib/format";
import { usePalette } from "../lib/theme";

export default function Maritime() {
  const pal = usePalette();
  const sites = useSites();
  const ports = (sites.data ?? []).filter((s) => s.kind === "port");
  const [siteId, setSiteId] = useState("");
  const id = siteId || ports[0]?.id;
  const objects = useObjects(id);
  const obs = useObservations(id);
  const [scene, setScene] = useState("");

  const vessels = useMemo(() => (objects.data ?? []).filter((o) => o.class === "vessel"), [objects.data]);
  const scenes = [...new Set(vessels.map((v) => v.scene_id))].sort().reverse();
  const current = scene || scenes[0];
  const inScene = vessels.filter((v) => v.scene_id === current);
  const series = obs.data?.series["vessel_count"] ?? [];

  if (sites.data && !ports.length)
    return <Empty title="No port sites are monitored">Add a site of kind “port” to use the maritime view.</Empty>;

  return (
    <div className="space-y-4">
      <PageHeader
        title="Maritime"
        subtitle="Vessel detections at monitored ports, counted per pass."
        actions={
          <select className="input w-auto" value={id ?? ""} onChange={(e) => setSiteId(e.target.value)}>
            {ports.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        }
      />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Vessel detections" value={fmtNum(vessels.length)} sub="all passes" tone="info" />
        <Stat label="Passes with vessels" value={scenes.length} />
        <Stat label="In selected pass" value={inScene.length} tone="accent" />
        <Stat label="Mean confidence" value={inScene.length ? fmtPct(inScene.reduce((s, v) => s + v.confidence, 0) / inScene.length) : "—"} />
      </div>
      <div className="grid gap-4 xl:grid-cols-[1.5fr_1fr]">
        <Card
          title="Vessels in pass"
          bodyClass="p-2"
          actions={
            <select className="input w-auto py-1 text-xs" value={current ?? ""} onChange={(e) => setScene(e.target.value)}>
              {scenes.map((s) => <option key={s} value={s}>{s.split(":")[0]} · {s.split(":").at(-1)}</option>)}
            </select>
          }
        >
          <Q q={objects} empty={() => !vessels.length && <Empty title="No vessels detected at this site" />}>
            {() => (
              <WorldMap
                height={420}
                points={inScene.map((v) => ({ id: v.id, lon: v.lon, lat: v.lat, tone: "object", size: 4, label: `vessel · ${fmtNum(v.extent_m)} m · ${fmtPct(v.confidence)}` }))}
              />
            )}
          </Q>
        </Card>
        <Card title="Vessel count per pass">
          {series.length ? (
            <ResponsiveContainer width="100%" height={260}>
              <LineChart data={series} margin={{ top: 8, right: 8, left: -18, bottom: 0 }}>
                <CartesianGrid stroke={pal.line} strokeDasharray="2 4" />
                <XAxis dataKey="date" tick={{ fill: pal.dim, fontSize: 10 }} stroke={pal.line} tickFormatter={(x) => fmtDate(x).slice(0, 6)} />
                <YAxis tick={{ fill: pal.dim, fontSize: 10 }} stroke={pal.line} allowDecimals={false} />
                <Tooltip contentStyle={{ background: pal.panel, border: `1px solid ${pal.line}`, fontSize: 12 }} />
                <Line dataKey="value" stroke={pal.info} strokeWidth={2} dot={{ r: 2.5 }} name="vessels" />
              </LineChart>
            </ResponsiveContainer>
          ) : (
            <Empty title="No counts in this window" />
          )}
        </Card>
      </div>
      <Card title="AIS correlation">
        <div className="space-y-2 text-sm">
          <p>
            The engine correlates SAR vessel detections with AIS reports (<span className="mono">ais.py</span>). It is not yet exposed over HTTP; the
            proposed route is <span className="mono">GET /api/sites/{"{id}"}/ais?scene=</span>, returning each detection as correlated, uncorrelated, or
            no-coverage.
          </p>
          <Caveat>
            A detection with no matching AIS report is <b>uncorrelated</b> — not dark, evading or suspicious. Class B transponders are missed beyond
            ~20 nm, satellite AIS has gaps, many fishing vessels are not required to carry AIS, and wave facets and buoys return bright on SAR. The
            console will show the search window and those explanations with every such finding.
          </Caveat>
        </div>
      </Card>
    </div>
  );
}
