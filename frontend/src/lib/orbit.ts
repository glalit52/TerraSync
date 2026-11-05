import {
  degreesLat,
  degreesLong,
  degreesToRadians,
  ecfToLookAngles,
  eciToEcf,
  eciToGeodetic,
  gstime,
  propagate,
  twoline2satrec,
  type SatRec,
} from "satellite.js";

// Orbit arithmetic for the SDA screen, done in the browser from two-line
// element sets. SGP4 is the standard propagator for TLEs; its error grows by
// roughly a kilometre or more per day away from epoch, so every result carries
// the age of the elements it came from.

export interface Tle {
  name: string;
  line1: string;
  line2: string;
}

export interface Sat extends Tle {
  satrec: SatRec;
  norad: string;
  epoch: Date;
  periodMin: number;
  inclinationDeg: number;
}

export interface Pass {
  sat: string;
  site: string;
  start: Date;
  end: Date;
  peak: Date;
  maxElevationDeg: number;
}

export function parseTles(text: string): Tle[] {
  const lines = text
    .split(/\r?\n/)
    .map((l) => l.trimEnd())
    .filter((l) => l.trim());
  const out: Tle[] = [];
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    if (l.startsWith("1 ") && lines[i + 1]?.startsWith("2 ")) {
      out.push({ name: `NORAD ${l.slice(2, 7).trim()}`, line1: l, line2: lines[i + 1] });
      i += 1;
    } else if (lines[i + 1]?.startsWith("1 ") && lines[i + 2]?.startsWith("2 ")) {
      out.push({ name: l.replace(/^0 /, "").trim(), line1: lines[i + 1], line2: lines[i + 2] });
      i += 2;
    }
  }
  return out;
}

/** TLE checksum: digits summed, '-' counts 1, mod 10. */
export function checksumOk(line: string): boolean {
  if (line.length < 69) return false;
  let sum = 0;
  for (const ch of line.slice(0, 68)) {
    if (ch >= "0" && ch <= "9") sum += Number(ch);
    else if (ch === "-") sum += 1;
  }
  return sum % 10 === Number(line[68]);
}

function epochOf(line1: string): Date {
  const yy = Number(line1.slice(18, 20));
  const day = Number(line1.slice(20, 32));
  const year = yy < 57 ? 2000 + yy : 1900 + yy;
  return new Date(Date.UTC(year, 0, 1) + (day - 1) * 86_400_000);
}

export function toSat(t: Tle): Sat {
  const satrec = twoline2satrec(t.line1, t.line2);
  const meanMotion = Number(t.line2.slice(52, 63)); // revs per day
  return {
    ...t,
    satrec,
    norad: t.line1.slice(2, 7).trim(),
    epoch: epochOf(t.line1),
    periodMin: meanMotion ? 1440 / meanMotion : NaN,
    inclinationDeg: Number(t.line2.slice(8, 16)),
  };
}

export function subpoint(sat: Sat, when: Date): { lon: number; lat: number; altKm: number } | null {
  const pv = propagate(sat.satrec, when);
  if (!pv || typeof pv.position === "boolean" || !pv.position) return null;
  const gd = eciToGeodetic(pv.position, gstime(when));
  return { lon: degreesLong(gd.longitude), lat: degreesLat(gd.latitude), altKm: gd.height };
}

/** Ground track as line segments, split where it crosses the antimeridian. */
export function groundTrack(sat: Sat, from: Date, minutes: number, stepMin = 1): [number, number][][] {
  const segs: [number, number][][] = [[]];
  for (let m = 0; m <= minutes; m += stepMin) {
    const p = subpoint(sat, new Date(from.getTime() + m * 60_000));
    if (!p) continue;
    const cur = segs[segs.length - 1];
    const prev = cur[cur.length - 1];
    if (prev && Math.abs(prev[0] - p.lon) > 180) segs.push([]);
    segs[segs.length - 1].push([p.lon, p.lat]);
  }
  return segs.filter((s) => s.length > 1);
}

export function elevationDeg(sat: Sat, when: Date, lon: number, lat: number): number | null {
  const pv = propagate(sat.satrec, when);
  if (!pv || typeof pv.position === "boolean" || !pv.position) return null;
  const ecf = eciToEcf(pv.position, gstime(when));
  const look = ecfToLookAngles({ longitude: degreesToRadians(lon), latitude: degreesToRadians(lat), height: 0 }, ecf);
  return (look.elevation * 180) / Math.PI;
}

/** Windows in which the satellite is above `minElevation` degrees from a site. */
export function passes(
  sat: Sat,
  site: { id: string; lon: number; lat: number },
  from: Date,
  hours: number,
  minElevation = 10,
  stepSec = 30,
): Pass[] {
  const out: Pass[] = [];
  let open: Pass | null = null;
  const steps = Math.ceil((hours * 3600) / stepSec);
  for (let i = 0; i <= steps; i++) {
    const t = new Date(from.getTime() + i * stepSec * 1000);
    const el = elevationDeg(sat, t, site.lon, site.lat);
    if (el !== null && el >= minElevation) {
      if (!open) open = { sat: sat.name, site: site.id, start: t, end: t, peak: t, maxElevationDeg: el };
      open.end = t;
      if (el > open.maxElevationDeg) {
        open.maxElevationDeg = el;
        open.peak = t;
      }
    } else if (open) {
      out.push(open);
      open = null;
    }
  }
  if (open) out.push(open);
  return out;
}

export function ageDays(sat: Sat, at: Date): number {
  return (at.getTime() - sat.epoch.getTime()) / 86_400_000;
}

// The worked example published with satellite.js, kept as a self-test that
// the propagator runs. Its epoch is June 2019: it demonstrates the arithmetic
// and is NOT a current position. Paste current elements from your catalogue.
export const SAMPLE_TLE = `ISS (ZARYA) — sample, epoch 2019-06-05
1 25544U 98067A   19156.50900463  .00003075  00000-0  59442-4 0  9992
2 25544  51.6433  59.2583 0008217  16.4489 347.6017 15.51174618173442`;
