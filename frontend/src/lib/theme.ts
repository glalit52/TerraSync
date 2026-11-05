import { useEffect, useState } from "react";

// Theme colours as concrete strings. SVG presentation attributes do not
// reliably resolve CSS custom properties, and the chart library writes them as
// attributes, so the palette is read from the root element and re-read
// whenever the theme class changes.

const KEYS = ["bg", "panel", "raised", "line", "ink", "dim", "accent", "crit", "high", "med", "low", "info"] as const;
export type Palette = Record<(typeof KEYS)[number], string>;

function read(): Palette {
  const cs = getComputedStyle(document.documentElement);
  const out = {} as Palette;
  KEYS.forEach((k) => {
    const v = cs.getPropertyValue(`--${k}`).trim();
    out[k] = v ? `rgb(${v.split(/\s+/).join(",")})` : "#888";
  });
  return out;
}

export function usePalette(): Palette {
  const [p, setP] = useState<Palette>(read);
  useEffect(() => {
    const mo = new MutationObserver(() => setP(read()));
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => mo.disconnect();
  }, []);
  return p;
}

export function severityColor(p: Palette, s: string): string {
  return ({ critical: p.crit, high: p.high, medium: p.med, low: p.low } as Record<string, string>)[s] ?? p.dim;
}
