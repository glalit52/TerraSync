import type { Change, LonLat, Severity } from "../api/types";

// Asset-level intelligence: answering "what has the monitoring record seen
// near this place?" for one point, a portfolio, or an event footprint.
//
// Everything here is computed from TerraShield's own findings. There is no
// hazard model, no flood zone, no valuation. A score here measures observed
// change near an asset, which is evidence for a human decision about it, not
// a risk rating of it.

const R_EARTH_M = 6_371_008.8;
const rad = (d: number) => (d * Math.PI) / 180;

export function distanceM(a: LonLat, b: LonLat): number {
  const dLat = rad(b[1] - a[1]);
  const dLon = rad(b[0] - a[0]);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a[1])) * Math.cos(rad(b[1])) * Math.sin(dLon / 2) ** 2;
  return 2 * R_EARTH_M * Math.asin(Math.min(1, Math.sqrt(h)));
}

/** A circle of `radiusM` around a point, as a ring, for drawing a search radius. */
export function circleRing(p: LonLat, radiusM: number, steps = 48): LonLat[] {
  const dLat = (radiusM / R_EARTH_M) * (180 / Math.PI);
  const dLon = dLat / Math.cos(rad(p[1]));
  return Array.from({ length: steps }, (_, i) => {
    const a = (i / steps) * 2 * Math.PI;
    return [p[0] + dLon * Math.cos(a), p[1] + dLat * Math.sin(a)] as LonLat;
  });
}

/** Vertex mean. Adequate for the small, compact polygons findings have. */
export function centroid(ring: LonLat[]): LonLat {
  if (!ring.length) return [NaN, NaN];
  const pts = ring.length > 1 && ring[0][0] === ring.at(-1)![0] && ring[0][1] === ring.at(-1)![1] ? ring.slice(0, -1) : ring;
  return [pts.reduce((s, p) => s + p[0], 0) / pts.length, pts.reduce((s, p) => s + p[1], 0) / pts.length];
}

/** Ray casting in lon/lat. Fine at site and city scale; not across the antimeridian. */
export function pointInPolygon(p: LonLat, ring: LonLat[]): boolean {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if (yi > p[1] !== yj > p[1] && p[0] < ((xj - xi) * (p[1] - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/** Nearest distance from a point to a finding: zero inside it, else to its nearest vertex or centroid. */
export function distanceToChange(p: LonLat, c: Change): number {
  if (c.geometry.length >= 3 && pointInPolygon(p, c.geometry)) return 0;
  const pts = [...c.geometry, centroid(c.geometry)];
  return Math.min(...pts.map((q) => distanceM(p, q)));
}

export interface Asset {
  id: string;
  name: string;
  lon: number;
  lat: number;
  value?: number;
  [extra: string]: string | number | undefined;
}

export interface CsvResult {
  assets: Asset[];
  errors: string[];
}

function splitCsvLine(line: string): string[] {
  const out: string[] = [];
  let cur = "";
  let quoted = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (ch === '"') {
      if (quoted && line[i + 1] === '"') {
        cur += '"';
        i++;
      } else quoted = !quoted;
    } else if (ch === "," && !quoted) {
      out.push(cur);
      cur = "";
    } else cur += ch;
  }
  out.push(cur);
  return out.map((s) => s.trim());
}

const LON = ["lon", "lng", "long", "longitude", "x"];
const LAT = ["lat", "latitude", "y"];
const NAME = ["name", "asset", "asset_name", "title", "address", "site"];
const VALUE = ["value", "tiv", "insured_value", "sum_insured", "exposure"];

/**
 * Parse a portfolio CSV. Columns are found by name, case-insensitively, so a
 * schedule exported from a policy system loads without editing. Rows that
 * cannot be placed are reported, never silently dropped: a portfolio that
 * quietly loses rows reports those assets as unexposed.
 */
export function parseAssetsCsv(text: string): CsvResult {
  const lines = text.split(/\r?\n/).filter((l) => l.trim());
  if (!lines.length) return { assets: [], errors: ["The file is empty."] };
  const header = splitCsvLine(lines[0]).map((h) => h.toLowerCase());
  const find = (names: string[]) => header.findIndex((h) => names.includes(h));
  const iLon = find(LON);
  const iLat = find(LAT);
  const iName = find(NAME);
  const iVal = find(VALUE);
  const iId = header.indexOf("id");
  if (iLon < 0 || iLat < 0) {
    return { assets: [], errors: [`No longitude/latitude columns found. Expected one of ${LON.join(", ")} and one of ${LAT.join(", ")}.`] };
  }
  const assets: Asset[] = [];
  const errors: string[] = [];
  lines.slice(1).forEach((line, k) => {
    const row = splitCsvLine(line);
    const lon = Number(row[iLon]);
    const lat = Number(row[iLat]);
    const n = k + 2;
    if (!Number.isFinite(lon) || !Number.isFinite(lat) || Math.abs(lon) > 180 || Math.abs(lat) > 90) {
      errors.push(`Row ${n}: cannot read a coordinate from "${row[iLon]}", "${row[iLat]}".`);
      return;
    }
    const value = iVal >= 0 ? Number(String(row[iVal]).replace(/[^0-9.-]/g, "")) : undefined;
    const a: Asset = {
      id: (iId >= 0 && row[iId]) || `A${String(n - 1).padStart(4, "0")}`,
      name: (iName >= 0 && row[iName]) || `Asset ${n - 1}`,
      lon,
      lat,
      value: value !== undefined && Number.isFinite(value) ? value : undefined,
    };
    header.forEach((h, i) => {
      if (![iLon, iLat, iName, iVal, iId].includes(i) && row[i] !== undefined) a[h] = row[i];
    });
    assets.push(a);
  });
  return { assets, errors };
}

const SEV_WEIGHT: Record<Severity, number> = { low: 1, medium: 3, high: 6, critical: 10 };
const SEV_RANK: Record<Severity, number> = { low: 1, medium: 2, high: 3, critical: 4 };

export interface Screening {
  asset: Asset;
  nearby: { change: Change; distanceM: number }[];
  count: number;
  confirmed: number;
  worst: Severity | null;
  nearestM: number | null;
  /** 0–100. Observed change near the asset; see SCORE_METHOD. */
  score: number;
  reasons: string[];
}

export const SCORE_METHOD =
  "Each finding within the radius adds its severity weight (low 1, medium 3, high 6, critical 10), " +
  "scaled by detection confidence and by proximity (full weight inside the finding, falling linearly to a quarter at the radius edge). " +
  "Rejected findings are excluded; confirmed ones count 1.5×. The sum maps to 0–100 as 100·(1 − e^(−sum/12)). " +
  "It measures observed change near the asset, not the likelihood of loss.";

export function screenAsset(asset: Asset, changes: Change[], radiusM: number): Screening {
  const p: LonLat = [asset.lon, asset.lat];
  const nearby = changes
    .filter((c) => c.review_status !== "rejected")
    .map((change) => ({ change, distanceM: distanceToChange(p, change) }))
    .filter((x) => x.distanceM <= radiusM)
    .sort((a, b) => a.distanceM - b.distanceM);

  let sum = 0;
  nearby.forEach(({ change, distanceM: d }) => {
    const proximity = d === 0 ? 1 : 1 - 0.75 * (d / radiusM);
    const review = change.review_status === "confirmed" ? 1.5 : 1;
    sum += SEV_WEIGHT[change.severity] * change.confidence * proximity * review;
  });
  const score = Math.round(100 * (1 - Math.exp(-sum / 12)));

  const worst = nearby.reduce<Severity | null>(
    (w, { change }) => (!w || SEV_RANK[change.severity] > SEV_RANK[w] ? change.severity : w),
    null,
  );
  const confirmed = nearby.filter((x) => x.change.review_status === "confirmed").length;
  const inside = nearby.filter((x) => x.distanceM === 0).length;
  const types = [...new Set(nearby.map((x) => x.change.type.replace(/_/g, " ")))];
  const reasons: string[] = [];
  if (inside) reasons.push(`${inside} finding(s) overlap the asset location`);
  if (nearby.length) reasons.push(`${nearby.length} finding(s) within ${Math.round(radiusM)} m: ${types.slice(0, 3).join(", ")}${types.length > 3 ? "…" : ""}`);
  if (confirmed) reasons.push(`${confirmed} confirmed by an analyst`);

  return {
    asset,
    nearby,
    count: nearby.length,
    confirmed,
    worst,
    nearestM: nearby.length ? nearby[0].distanceM : null,
    score,
    reasons,
  };
}

/** Change types that describe physical damage or water, for event-footprint triage. */
export const DAMAGE_TYPES = new Set(["possible_damage", "inundation", "structure_removed", "surface_change"]);

export function toCsv(rows: Record<string, unknown>[]): string {
  if (!rows.length) return "";
  const cols = Object.keys(rows[0]);
  const esc = (v: unknown) => {
    const s = v === null || v === undefined ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return [cols.join(","), ...rows.map((r) => cols.map((c) => esc(r[c])).join(","))].join("\n");
}

// Placed on and around the demo estate's findings, plus one asset nowhere near
// a monitored site, which must screen as "not covered" rather than as safe.
export const SAMPLE_PORTFOLIO = `id,name,lon,lat,value
P-001,Mundra terminal warehouse,69.7028,22.7430,4200000
P-002,Mundra berth crane line,69.7134,22.7518,9800000
P-003,Mundra gate complex,69.6900,22.7300,1500000
P-004,Bhadla PV block inverter station,71.9109,27.5454,2600000
P-005,Bhadla substation,71.8900,27.5300,7300000
P-006,Sardar Sarovar gate house,73.7542,21.8231,5100000
P-007,Kutch sector depot,70.5287,23.9148,600000
P-008,Ahmedabad logistics park,72.5700,23.0300,3100000`;
