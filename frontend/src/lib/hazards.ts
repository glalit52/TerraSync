import type { LonLat } from "../api/types";
import { pointInPolygon } from "./assets";

// Hazard exposure from layers the deployment supplies: a national flood map,
// a fault database, a wildfire hazard grid as polygons, a landslide
// susceptibility map. TerraShield ships no hazard data of its own. A result
// here is only as good as the layer behind it, so every result names its layer
// and its source.

export type HazardKind = "flood" | "wildfire" | "landslide" | "earthquake" | "other";

/** How a layer is read at a point. */
export type LayerMode =
  /** Polygons carrying a class: the containing polygon's class is the answer. */
  | "zone"
  /** Lines (faults, coastlines): the distance to the nearest line is the answer. */
  | "distance";

export interface HazardLayer {
  id: string;
  name: string;
  kind: HazardKind;
  mode: LayerMode;
  /** Feature property holding the class, for zone layers. */
  classField: string;
  /** Where the data came from. Shown beside every result. */
  source: string;
  synthetic: boolean;
  polygons: { rings: LonLat[][]; cls: string }[];
  lines: LonLat[][];
}

export interface HazardResult {
  layer: HazardLayer;
  /** Zone class, or "outside mapped zones"; for distance layers, metres. */
  value: string;
  distanceM?: number;
  /** True when the result should raise attention (in a zone, or near a line). */
  flagged: boolean;
}

type Geom =
  | { type: "Polygon"; coordinates: LonLat[][] }
  | { type: "MultiPolygon"; coordinates: LonLat[][][] }
  | { type: "LineString"; coordinates: LonLat[] }
  | { type: "MultiLineString"; coordinates: LonLat[][] }
  | { type: string; coordinates: unknown };

interface Feature {
  type: "Feature";
  geometry: Geom | null;
  properties?: Record<string, unknown> | null;
}

/** Parse a GeoJSON FeatureCollection into a layer. Unsupported geometries are counted, not hidden. */
export function layerFromGeoJson(
  text: string,
  meta: { name: string; kind: HazardKind; mode: LayerMode; classField: string; source: string },
): { layer?: HazardLayer; error?: string; skipped: number } {
  let doc: { type?: string; features?: Feature[] };
  try {
    doc = JSON.parse(text);
  } catch (e) {
    return { error: `Not valid JSON: ${(e as Error).message}`, skipped: 0 };
  }
  const features: Feature[] = doc.type === "FeatureCollection" ? doc.features ?? [] : doc.type === "Feature" ? [doc as unknown as Feature] : [];
  if (!features.length) return { error: "Expected a GeoJSON FeatureCollection with at least one feature.", skipped: 0 };

  const polygons: HazardLayer["polygons"] = [];
  const lines: HazardLayer["lines"] = [];
  let skipped = 0;
  for (const f of features) {
    const g = f.geometry;
    const cls = String(f.properties?.[meta.classField] ?? "unclassified");
    if (!g) {
      skipped++;
      continue;
    }
    if (g.type === "Polygon") polygons.push({ rings: g.coordinates as LonLat[][], cls });
    else if (g.type === "MultiPolygon") (g.coordinates as LonLat[][][]).forEach((p) => polygons.push({ rings: p, cls }));
    else if (g.type === "LineString") lines.push(g.coordinates as LonLat[]);
    else if (g.type === "MultiLineString") (g.coordinates as LonLat[][]).forEach((l) => lines.push(l));
    else skipped++;
  }
  if (meta.mode === "zone" && !polygons.length) return { error: "A zone layer needs Polygon or MultiPolygon features.", skipped };
  if (meta.mode === "distance" && !lines.length) return { error: "A distance layer needs LineString or MultiLineString features.", skipped };
  return {
    layer: { id: `lyr-${Date.now().toString(36)}`, ...meta, synthetic: false, polygons, lines },
    skipped,
  };
}

/** Inside the outer ring and outside every hole. */
function inPolygonWithHoles(p: LonLat, rings: LonLat[][]): boolean {
  if (!rings.length || !pointInPolygon(p, rings[0])) return false;
  return !rings.slice(1).some((hole) => pointInPolygon(p, hole));
}

/** Point-to-segment distance in metres, on a local equirectangular plane (accurate to well under 1% below ~100 km). */
function segmentDistanceM(p: LonLat, a: LonLat, b: LonLat): number {
  const k = 111_320;
  const cos = Math.cos((p[1] * Math.PI) / 180);
  const ax = (a[0] - p[0]) * k * cos;
  const ay = (a[1] - p[1]) * k;
  const bx = (b[0] - p[0]) * k * cos;
  const by = (b[1] - p[1]) * k;
  const dx = bx - ax;
  const dy = by - ay;
  const len2 = dx * dx + dy * dy;
  const t = len2 ? Math.max(0, Math.min(1, -(ax * dx + ay * dy) / len2)) : 0;
  return Math.hypot(ax + t * dx, ay + t * dy);
}

export function distanceToLinesM(p: LonLat, lines: LonLat[][]): number {
  let best = Infinity;
  for (const line of lines) for (let i = 1; i < line.length; i++) best = Math.min(best, segmentDistanceM(p, line[i - 1], line[i]));
  return best;
}

/** Distance under which a line layer flags a site. 10 km is a common screening distance for active faults. */
export const NEAR_LINE_M = 10_000;

/** Classes read as "no hazard" when a layer maps them explicitly. */
const BENIGN = /^(none|minimal|very ?low|outside|x|zone x|unclassified)$/i;

export function assess(p: LonLat, layer: HazardLayer): HazardResult {
  if (layer.mode === "zone") {
    const hit = layer.polygons.find((poly) => inPolygonWithHoles(p, poly.rings));
    return hit
      ? { layer, value: hit.cls, flagged: !BENIGN.test(hit.cls.trim()) }
      : { layer, value: "outside mapped zones", flagged: false };
  }
  const d = distanceToLinesM(p, layer.lines);
  return { layer, value: Number.isFinite(d) ? `${(d / 1000).toFixed(1)} km` : "no lines", distanceM: d, flagged: d <= NEAR_LINE_M };
}

// ---------------------------------------------------------------------------
// Synthetic demonstration layers
// ---------------------------------------------------------------------------

function rect(w: number, s: number, e: number, n: number): LonLat[][] {
  return [[[w, s], [e, s], [e, n], [w, n], [w, s]]];
}

/**
 * Invented layers placed over the demo estate so the workflow can be shown.
 * They are NOT hazard data: the shapes, classes and lines are made up, and the
 * console says so wherever one of them contributes to a result.
 */
export function syntheticDemoLayers(): HazardLayer[] {
  const src = "Synthetic — invented for the demo estate. Not hazard data.";
  return [
    {
      id: "demo-flood",
      name: "Flood zones (synthetic demo)",
      kind: "flood",
      mode: "zone",
      classField: "zone",
      source: src,
      synthetic: true,
      polygons: [
        { rings: rect(69.695, 22.735, 69.712, 22.748), cls: "High" },
        { rings: rect(73.745, 21.815, 73.760, 21.830), cls: "High" },
        { rings: rect(69.680, 22.720, 69.730, 22.760), cls: "Moderate" },
      ],
      lines: [],
    },
    {
      id: "demo-landslide",
      name: "Landslide susceptibility (synthetic demo)",
      kind: "landslide",
      mode: "zone",
      classField: "class",
      source: src,
      synthetic: true,
      polygons: [{ rings: rect(73.726, 21.816, 73.770, 21.846), cls: "Moderate" }],
      lines: [],
    },
    {
      id: "demo-fault",
      name: "Fault lines (synthetic demo)",
      kind: "earthquake",
      mode: "distance",
      classField: "",
      source: src,
      synthetic: true,
      polygons: [],
      lines: [
        [[70.20, 23.70], [70.60, 23.95], [71.00, 24.05]],
        [[69.40, 22.95], [69.95, 23.20]],
      ],
    },
  ];
}

export const HAZARD_LABEL: Record<HazardKind, string> = {
  flood: "Flood",
  wildfire: "Wildfire",
  landslide: "Landslide",
  earthquake: "Earthquake",
  other: "Other",
};
