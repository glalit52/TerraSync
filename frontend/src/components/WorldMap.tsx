import { geoArea, geoMercator, geoPath, type GeoPermissibleObjects } from "d3-geo";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { feature } from "topojson-client";
import type { FeatureCollection, Geometry } from "geojson";
import { Crosshair, Minus, Plus } from "lucide-react";
import type { LonLat, Severity } from "../api/types";
import { usePalette, type Palette } from "../lib/theme";

// An offline map. The basemap is Natural Earth country outlines bundled into
// the build (world-atlas), so the console renders inside a facility with no
// route to a tile server. Imagery is not drawn here: the API serves findings
// and geometry, and the pixels stay with the evidence.

export interface MapPolygon {
  id: string;
  ring: LonLat[];
  tone?: Severity | "site" | "field" | "draft";
  label?: string;
}
export interface MapPoint {
  id: string;
  lon: number;
  lat: number;
  tone?: Severity | "site" | "object" | "sat";
  label?: string;
  pulse?: boolean;
  size?: number;
}
export interface MapLine {
  id: string;
  coords: LonLat[];
  tone?: "sat" | "accent";
}

const toneOf = (p: Palette, t: string): string =>
  ({
    critical: p.crit,
    high: p.high,
    medium: p.med,
    low: p.low,
    site: p.accent,
    field: p.info,
    draft: p.info,
    object: p.info,
    sat: p.info,
    accent: p.accent,
  } as Record<string, string>)[t] ?? p.accent;

let landCache: Promise<FeatureCollection<Geometry>> | null = null;
function loadLand(): Promise<FeatureCollection<Geometry>> {
  // Split into its own chunk: the outlines are the largest asset in the build
  // and the sign-in screen does not need them.
  landCache ??= import("world-atlas/countries-50m.json").then((m) => {
    const topo = (m as { default: unknown }).default as Parameters<typeof feature>[0];
    const objects = (topo as unknown as { objects: { countries: Parameters<typeof feature>[1] } }).objects;
    return feature(topo, objects.countries) as unknown as FeatureCollection<Geometry>;
  });
  return landCache;
}

function boundsOf(polys: MapPolygon[], points: MapPoint[]): [[number, number], [number, number]] | null {
  const xs: number[] = [];
  const ys: number[] = [];
  polys.forEach((p) => p.ring.forEach(([x, y]) => (xs.push(x), ys.push(y))));
  points.forEach((p) => (xs.push(p.lon), ys.push(p.lat)));
  if (!xs.length) return null;
  return [
    [Math.min(...xs), Math.min(...ys)],
    [Math.max(...xs), Math.max(...ys)],
  ];
}

export function WorldMap({
  polygons = [],
  points = [],
  lines = [],
  focus,
  height = 420,
  selected,
  onSelect,
  drawing,
  onDrawPoint,
  overlay,
}: {
  polygons?: MapPolygon[];
  points?: MapPoint[];
  lines?: MapLine[];
  /** [[west, south], [east, north]]; defaults to the extent of what is drawn. */
  focus?: [[number, number], [number, number]] | null;
  height?: number | string;
  selected?: string | null;
  onSelect?: (id: string) => void;
  drawing?: boolean;
  onDrawPoint?: (p: LonLat) => void;
  overlay?: ReactNode;
}) {
  const pal = usePalette();
  const wrap = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 800, h: 400 });
  const [land, setLand] = useState<FeatureCollection<Geometry> | null>(null);
  const [view, setView] = useState({ k: 1, x: 0, y: 0 });
  const [cursor, setCursor] = useState<LonLat | null>(null);
  const drag = useRef<{ x: number; y: number; moved: boolean } | null>(null);

  useEffect(() => {
    loadLand().then(setLand).catch(() => setLand(null));
  }, []);

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setSize({ w: e.contentRect.width, h: e.contentRect.height }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const extent = useMemo(() => {
    const b = focus ?? boundsOf(polygons, points) ?? [[60, 5], [100, 38]];
    // Pad small extents so a single 15 km² site is shown in context.
    const [[w, s], [e, n]] = b;
    // An explicit focus is framed tightly (a 900 m² change must fill the view);
    // a derived one keeps a floor so a lone point still shows its surroundings.
    const pad = Math.max(focus ? 0.0002 : 0.004, (e - w) * 0.2, (n - s) * 0.2);
    // Clamp to what Mercator can draw: past ±85° latitude it diverges.
    const W = Math.max(-180, w - pad), E = Math.min(180, e + pad);
    const S = Math.max(-85, s - pad), N = Math.min(85, n + pad);
    return { type: "MultiPoint", coordinates: [[W, S], [E, N]] } as GeoPermissibleObjects;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus ? JSON.stringify(focus) : polygons.length + ":" + points.length]);

  const projection = useMemo(
    () => geoMercator().fitExtent([[16, 16], [Math.max(32, size.w - 16), Math.max(32, size.h - 16)]], extent),
    [extent, size.w, size.h],
  );
  const path = useMemo(() => geoPath(projection), [projection]);

  // Reset pan/zoom when the focus changes, so "zoom to site" always lands.
  useEffect(() => {
    setView({ k: 1, x: 0, y: 0 });
  }, [extent]);

  const toLonLat = (clientX: number, clientY: number): LonLat | null => {
    const r = wrap.current?.getBoundingClientRect();
    if (!r) return null;
    const px = (clientX - r.left - view.x) / view.k;
    const py = (clientY - r.top - view.y) / view.k;
    const ll = projection.invert?.([px, py]);
    return ll ? [ll[0], ll[1]] : null;
  };

  const zoomAt = (factor: number, cx = size.w / 2, cy = size.h / 2) =>
    setView((v) => {
      const k = Math.max(0.5, Math.min(4000, v.k * factor));
      const f = k / v.k;
      return { k, x: cx - (cx - v.x) * f, y: cy - (cy - v.y) * f };
    });

  const onWheel = (e: React.WheelEvent) => {
    const r = wrap.current!.getBoundingClientRect();
    zoomAt(e.deltaY < 0 ? 1.25 : 0.8, e.clientX - r.left, e.clientY - r.top);
  };

  // d3-geo reads rings on the sphere, where winding decides which side is
  // inside. GeoJSON from the API is planar-convention, so a ring that comes
  // back covering more than a hemisphere is the complement and is reversed.
  const ringPath = (ring: LonLat[]) => {
    if (ring.length < 3) return "";
    let closed = [...ring, ring[0]];
    if (geoArea({ type: "Polygon", coordinates: [closed] } as GeoPermissibleObjects) > 2 * Math.PI) closed = closed.reverse();
    return path({ type: "Polygon", coordinates: [closed] } as GeoPermissibleObjects) ?? "";
  };

  const r = (base: number) => base / Math.sqrt(view.k) / Math.max(1, Math.sqrt(view.k) / 2);

  return (
    <div
      ref={wrap}
      className="map-grid relative select-none overflow-hidden rounded-md bg-bg"
      style={{ height, cursor: drawing ? "crosshair" : drag.current ? "grabbing" : "grab" }}
      onWheel={onWheel}
      onPointerDown={(e) => {
        (e.target as Element).setPointerCapture?.(e.pointerId);
        drag.current = { x: e.clientX, y: e.clientY, moved: false };
      }}
      onPointerMove={(e) => {
        setCursor(toLonLat(e.clientX, e.clientY));
        const d = drag.current;
        if (!d) return;
        const dx = e.clientX - d.x;
        const dy = e.clientY - d.y;
        if (Math.abs(dx) + Math.abs(dy) > 3) d.moved = true;
        if (d.moved) {
          setView((v) => ({ ...v, x: v.x + dx, y: v.y + dy }));
          d.x = e.clientX;
          d.y = e.clientY;
        }
      }}
      onPointerUp={(e) => {
        const d = drag.current;
        drag.current = null;
        if (d && !d.moved && drawing && onDrawPoint) {
          const ll = toLonLat(e.clientX, e.clientY);
          if (ll) onDrawPoint([+ll[0].toFixed(6), +ll[1].toFixed(6)]);
        }
      }}
      onPointerLeave={() => setCursor(null)}
      role="application"
      aria-label="Map"
    >
      <svg width={size.w} height={size.h} className="absolute inset-0">
        <g transform={`translate(${view.x},${view.y}) scale(${view.k})`}>
          {land?.features.map((f, i) => (
            <path
              key={i}
              d={path(f) ?? ""}
              fill={pal.raised}
              stroke={pal.line}
              strokeWidth={0.8}
              vectorEffect="non-scaling-stroke"
            />
          ))}
          {lines.map((l) => (
            <path
              key={l.id}
              d={path({ type: "LineString", coordinates: l.coords } as GeoPermissibleObjects) ?? ""}
              fill="none"
              stroke={toneOf(pal, l.tone ?? "sat")}
              strokeWidth={1.4}
              strokeDasharray="4 3"
              vectorEffect="non-scaling-stroke"
              opacity={0.85}
            />
          ))}
          {polygons.map((p) => {
            const c = toneOf(pal, p.tone ?? "site");
            const isSel = selected === p.id;
            return (
              <path
                key={p.id}
                d={ringPath(p.ring)}
                fill={c}
                fillOpacity={p.tone === "draft" ? 0.15 : isSel ? 0.35 : 0.18}
                stroke={c}
                strokeWidth={isSel ? 2.5 : 1.2}
                strokeDasharray={p.tone === "draft" ? "5 3" : undefined}
                vectorEffect="non-scaling-stroke"
                onClick={(e) => {
                  e.stopPropagation();
                  onSelect?.(p.id);
                }}
                className="cursor-pointer"
              >
                {p.label && <title>{p.label}</title>}
              </path>
            );
          })}
          {points.map((p) => {
            const xy = projection([p.lon, p.lat]);
            if (!xy) return null;
            const c = toneOf(pal, p.tone ?? "site");
            const rad = r(p.size ?? (p.tone === "object" ? 2.5 : 6));
            const isSel = selected === p.id;
            return (
              <g
                key={p.id}
                onClick={(e) => {
                  e.stopPropagation();
                  onSelect?.(p.id);
                }}
                className="cursor-pointer"
              >
                {p.pulse && <circle cx={xy[0]} cy={xy[1]} r={rad} fill={c} className="ping" />}
                <circle
                  cx={xy[0]}
                  cy={xy[1]}
                  r={isSel ? rad * 1.5 : rad}
                  fill={c}
                  fillOpacity={p.tone === "object" ? 0.75 : 0.9}
                  stroke={pal.bg}
                  strokeWidth={1}
                  vectorEffect="non-scaling-stroke"
                />
                {p.label && <title>{p.label}</title>}
              </g>
            );
          })}
        </g>
        {/* Labels in screen space so they do not scale with zoom. */}
        {points
          .filter((p) => p.label && p.tone !== "object")
          .map((p) => {
            const xy = projection([p.lon, p.lat]);
            if (!xy) return null;
            return (
              <text
                key={`l-${p.id}`}
                x={xy[0] * view.k + view.x + 10}
                y={xy[1] * view.k + view.y + 4}
                fontSize={11}
                fill={pal.ink}
                stroke={pal.bg}
                strokeWidth={3}
                paintOrder="stroke"
                className="pointer-events-none"
              >
                {p.label}
              </text>
            );
          })}
      </svg>

      <div className="absolute right-2 top-2 flex flex-col overflow-hidden rounded-md border border-line bg-panel/90">
        <button className="p-1.5 hover:bg-raised" onClick={() => zoomAt(1.6)} aria-label="Zoom in">
          <Plus className="h-4 w-4" />
        </button>
        <button className="border-t border-line p-1.5 hover:bg-raised" onClick={() => zoomAt(0.625)} aria-label="Zoom out">
          <Minus className="h-4 w-4" />
        </button>
        <button className="border-t border-line p-1.5 hover:bg-raised" onClick={() => setView({ k: 1, x: 0, y: 0 })} aria-label="Reset view">
          <Crosshair className="h-4 w-4" />
        </button>
      </div>
      {cursor && (
        <div className="pointer-events-none absolute bottom-2 right-2 rounded bg-panel/90 px-2 py-0.5 font-mono text-[10px] text-dim">
          {cursor[1].toFixed(4)}, {cursor[0].toFixed(4)}
        </div>
      )}
      <div className="pointer-events-none absolute bottom-2 left-2 text-[10px] text-dim">{land ? "Basemap: Natural Earth 1:50m (offline)" : "loading basemap…"}</div>
      {overlay}
    </div>
  );
}
