export const humanise = (s: string): string => s.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

export function fmtNum(n: number | null | undefined, digits = 0): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toLocaleString("en-IN", { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

export function fmtArea(m2: number): string {
  if (m2 >= 1_000_000) return `${fmtNum(m2 / 1_000_000, 2)} km²`;
  if (m2 >= 10_000) return `${fmtNum(m2 / 10_000, 1)} ha`;
  return `${fmtNum(m2)} m²`;
}

export const fmtPct = (x: number, digits = 0) => `${fmtNum(x * 100, digits)}%`;

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso.length === 10 ? `${iso}T00:00:00Z` : iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" });
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.toISOString().slice(0, 16).replace("T", " ")}Z`;
}

export function shortHash(h: string, n = 10): string {
  return h ? `${h.slice(0, n)}…` : "—";
}

export function fmtLonLat(lon: number, lat: number): string {
  const ns = lat >= 0 ? "N" : "S";
  const ew = lon >= 0 ? "E" : "W";
  return `${Math.abs(lat).toFixed(4)}°${ns} ${Math.abs(lon).toFixed(4)}°${ew}`;
}
