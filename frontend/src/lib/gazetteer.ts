import type { LonLat } from "../api/types";

// Place search that works offline. It resolves coordinates in the forms people
// paste, and the names this deployment already knows: monitored sites and
// their fields. Street addresses need a geocoder, which is an outbound service
// the console does not call; an address that resolves to nothing says so.

export interface Place {
  label: string;
  kind: "coordinate" | "site" | "field";
  lonlat: LonLat;
  ref?: string;
}

function dms(deg: string, min?: string, sec?: string, hemi?: string): number {
  const v = Number(deg) + Number(min ?? 0) / 60 + Number(sec ?? 0) / 3600;
  return /[SW]/i.test(hemi ?? "") ? -v : v;
}

/**
 * Coordinates in the forms that turn up in schedules and emails:
 * "22.743, 69.703" (lat, lon, the Google Maps order), "69.703 22.743" when the
 * first value cannot be a latitude, "22.743N 69.703E", and degrees-minutes-
 * seconds such as 22°44'34"N 69°42'10"E.
 */
export function parseCoordinate(text: string): LonLat | null {
  const t = text.trim();
  const dmsRe = /(\d+(?:\.\d+)?)\s*°\s*(?:(\d+(?:\.\d+)?)\s*['′]\s*)?(?:(\d+(?:\.\d+)?)\s*["″]\s*)?([NSEW])/gi;
  const parts = [...t.matchAll(dmsRe)];
  if (parts.length === 2) {
    const vals = parts.map((m) => ({ v: dms(m[1], m[2], m[3], m[4]), axis: /[NS]/i.test(m[4]) ? "lat" : "lon" }));
    const lat = vals.find((x) => x.axis === "lat");
    const lon = vals.find((x) => x.axis === "lon");
    if (lat && lon) return valid([lon.v, lat.v]);
  }
  const hemi = /^(-?\d+(?:\.\d+)?)\s*°?\s*([NS])[\s,;]+(-?\d+(?:\.\d+)?)\s*°?\s*([EW])$/i.exec(t);
  if (hemi) return valid([dms(hemi[3], undefined, undefined, hemi[4]), dms(hemi[1], undefined, undefined, hemi[2])]);
  const plain = /^(-?\d+(?:\.\d+)?)\s*[,;\s]\s*(-?\d+(?:\.\d+)?)$/.exec(t);
  if (plain) {
    const a = Number(plain[1]);
    const b = Number(plain[2]);
    // lat, lon unless the first value cannot be a latitude.
    return Math.abs(a) > 90 ? valid([a, b]) : valid([b, a]);
  }
  return null;
}

function valid(p: LonLat): LonLat | null {
  return Number.isFinite(p[0]) && Number.isFinite(p[1]) && Math.abs(p[0]) <= 180 && Math.abs(p[1]) <= 90 ? p : null;
}

export function searchPlaces(query: string, known: Place[]): Place[] {
  const coord = parseCoordinate(query);
  if (coord) return [{ label: `${coord[1].toFixed(5)}, ${coord[0].toFixed(5)}`, kind: "coordinate", lonlat: coord }];
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return [];
  return known.filter((p) => terms.every((t) => `${p.label} ${p.ref ?? ""}`.toLowerCase().includes(t))).slice(0, 8);
}
