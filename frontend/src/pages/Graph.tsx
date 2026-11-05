import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useAlerts, useChanges, useEvidencePath, useGraphStats, useSites, useSubgraph } from "../api/hooks";
import type { GraphEdge, GraphNode } from "../api/types";
import { Card, Empty, ErrorState, KeyValue, Loading, PageHeader, Tag } from "../components/ui";
import { humanise } from "../lib/format";
import { usePalette, type Palette } from "../lib/theme";

const KIND_COLOR = (p: Palette): Record<string, string> => ({
  area: p.accent,
  alert: p.crit,
  event: p.high,
  change: p.med,
  evidence: p.info,
  scene: p.low,
  source: p.dim,
  sensor: p.dim,
  model: p.accent,
  field: p.info,
});

interface Pos {
  x: number;
  y: number;
  vx: number;
  vy: number;
}

/** A small deterministic force layout: enough for a few hundred nodes, no dependency. */
function layout(nodes: GraphNode[], edges: GraphEdge[], root: string, w: number, h: number): Map<string, Pos> {
  const pos = new Map<string, Pos>();
  nodes.forEach((n, i) => {
    const a = (i / nodes.length) * Math.PI * 2;
    const r = n.id === root ? 0 : 120 + (i % 5) * 18;
    pos.set(n.id, { x: w / 2 + Math.cos(a) * r, y: h / 2 + Math.sin(a) * r, vx: 0, vy: 0 });
  });
  const ids = nodes.map((n) => n.id);
  for (let it = 0; it < 220; it++) {
    const t = 1 - it / 220;
    for (let i = 0; i < ids.length; i++) {
      const a = pos.get(ids[i])!;
      for (let j = i + 1; j < ids.length; j++) {
        const b = pos.get(ids[j])!;
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        const d2 = Math.max(25, dx * dx + dy * dy);
        const f = 1800 / d2;
        dx *= f / Math.sqrt(d2);
        dy *= f / Math.sqrt(d2);
        a.vx += dx;
        a.vy += dy;
        b.vx -= dx;
        b.vy -= dy;
      }
    }
    edges.forEach((e) => {
      const a = pos.get(e.from);
      const b = pos.get(e.to);
      if (!a || !b) return;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      const f = (d - 70) * 0.02;
      a.vx += (dx / d) * f;
      a.vy += (dy / d) * f;
      b.vx -= (dx / d) * f;
      b.vy -= (dy / d) * f;
    });
    pos.forEach((p, id) => {
      p.vx += (w / 2 - p.x) * 0.004;
      p.vy += (h / 2 - p.y) * 0.004;
      if (id === root) {
        p.vx = 0;
        p.vy = 0;
        p.x = w / 2;
        p.y = h / 2;
      }
      p.x += p.vx * t * 0.6;
      p.y += p.vy * t * 0.6;
      p.vx *= 0.5;
      p.vy *= 0.5;
      p.x = Math.max(20, Math.min(w - 20, p.x));
      p.y = Math.max(20, Math.min(h - 20, p.y));
    });
  }
  return pos;
}

export default function GraphPage() {
  const [params, setParams] = useSearchParams();
  const sites = useSites();
  const site = params.get("site") ?? sites.data?.[0]?.id ?? "";
  const alerts = useAlerts({ site, maxPriority: 4 });
  const changes = useChanges(site);
  const stats = useGraphStats(site);
  const defaultNode = alerts.data?.alerts[0]?.id ?? changes.data?.[0]?.id;
  const node = params.get("node") ?? defaultNode;
  const [depth, setDepth] = useState(3);
  const sub = useSubgraph(site, node, depth);
  const path = useEvidencePath(node, site);
  const pal = usePalette();
  const colors = KIND_COLOR(pal);
  const [hover, setHover] = useState<GraphNode | null>(null);
  const W = 900;
  const H = 560;

  const positions = useMemo(
    () => (sub.data ? layout(sub.data.nodes, sub.data.edges, node ?? "", W, H) : new Map<string, Pos>()),
    [sub.data, node],
  );
  const onPath = new Set(path.data?.path.map((n) => n.id) ?? []);

  useEffect(() => {
    setHover(null);
  }, [node]);

  const pick = (id: string) => setParams({ site, node: id });

  return (
    <div className="space-y-4">
      <PageHeader title="Evidence graph" subtitle="Alert → event → change → scene → source → sensor. Click any node to re-centre on it." />
      <div className="flex flex-wrap items-center gap-2">
        <select className="input w-auto" value={site} onChange={(e) => setParams({ site: e.target.value })} aria-label="Site">
          {sites.data?.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
        <select className="input w-auto" value={node ?? ""} onChange={(e) => pick(e.target.value)} aria-label="Start node">
          <optgroup label="Alerts">
            {alerts.data?.alerts.map((a) => <option key={a.id} value={a.id}>{a.title} · {a.id}</option>)}
          </optgroup>
          <optgroup label="Changes">
            {changes.data?.slice(0, 80).map((c) => <option key={c.id} value={c.id}>{humanise(c.type)} · {c.id}</option>)}
          </optgroup>
        </select>
        <label className="flex items-center gap-2 text-sm text-dim">
          Depth
          <input type="range" min={1} max={4} value={depth} onChange={(e) => setDepth(Number(e.target.value))} />
          {depth}
        </label>
      </div>
      <div className="grid gap-4 xl:grid-cols-[1fr_300px]">
        <Card bodyClass="p-0">
          {!node ? (
            <Empty title="Nothing to centre on">This site has no alerts or changes in the window.</Empty>
          ) : sub.isLoading ? (
            <Loading label="Building graph" />
          ) : sub.error ? (
            <ErrorState error={sub.error} />
          ) : sub.data ? (
            <svg viewBox={`0 0 ${W} ${H}`} className="h-[560px] w-full">
              {sub.data.edges.map((e, i) => {
                const a = positions.get(e.from);
                const b = positions.get(e.to);
                if (!a || !b) return null;
                const hot = onPath.has(e.from) && onPath.has(e.to);
                return (
                  <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={hot ? pal.accent : pal.line} strokeWidth={hot ? 2 : 1}>
                    <title>{e.rel}</title>
                  </line>
                );
              })}
              {sub.data.nodes.map((n) => {
                const p = positions.get(n.id);
                if (!p) return null;
                const isRoot = n.id === node;
                return (
                  <g key={n.id} onClick={() => pick(n.id)} onMouseEnter={() => setHover(n)} className="cursor-pointer">
                    <circle cx={p.x} cy={p.y} r={isRoot ? 11 : 7} fill={colors[n.kind] ?? pal.dim} stroke={onPath.has(n.id) ? pal.accent : pal.bg} strokeWidth={onPath.has(n.id) ? 2.5 : 1.5} />
                    {(isRoot || ["area", "alert", "event", "source", "sensor"].includes(n.kind)) && (
                      <text x={p.x + 12} y={p.y + 4} fontSize={10} fill={pal.ink} stroke={pal.panel} strokeWidth={3} paintOrder="stroke">
                        {n.label.length > 28 ? `${n.label.slice(0, 28)}…` : n.label}
                      </text>
                    )}
                  </g>
                );
              })}
            </svg>
          ) : null}
        </Card>
        <div className="space-y-4">
          <Card title="Legend">
            <div className="flex flex-wrap gap-1.5">
              {Object.entries(colors).map(([k, c]) => (
                <span key={k} className="chip border-line">
                  <span className="h-2 w-2 rounded-full" style={{ background: c }} /> {k}
                </span>
              ))}
            </div>
            <p className="mt-2 text-xs text-dim">Highlighted: the evidence path from the selected node back to its sources.</p>
          </Card>
          <Card title="Node">
            {hover ? (
              <KeyValue
                rows={[
                  ["Kind", <Tag key="k">{hover.kind}</Tag>],
                  ["Id", <span key="i" className="mono break-all">{hover.id}</span>],
                  ["Label", hover.label],
                  ...Object.entries(hover.attrs ?? {}).slice(0, 8).map(([k, v]) => [humanise(k), String(v)] as [string, string]),
                ]}
              />
            ) : (
              <p className="text-sm text-dim">Hover a node.</p>
            )}
          </Card>
          {stats.data && (
            <Card title="Site graph">
              <KeyValue rows={[["Nodes", stats.data.stats.nodes], ["Edges", stats.data.stats.edges]]} />
            </Card>
          )}
          <p className="text-[11px] text-dim">The ontology has no Person, Actor or Owner type, and no relation that expresses cause.</p>
        </div>
      </div>
    </div>
  );
}
