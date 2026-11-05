import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

// The analysis window every page reads. It is global on purpose: switching
// from the alert queue to a site page should not silently change what period
// the numbers describe.

export interface Window {
  from: string;
  to: string;
  label: string;
}

export function isoDay(d: Date): string {
  return d.toISOString().slice(0, 10);
}

export function presetWindow(days: number, end = new Date()): Window {
  const start = new Date(end.getTime() - days * 86_400_000);
  return { from: isoDay(start), to: isoDay(end), label: `Last ${days} days` };
}

export const PRESETS = [30, 90, 180, 365];

interface Ctx {
  window: Window;
  setWindow: (w: Window) => void;
}

const PeriodContext = createContext<Ctx | null>(null);

export function PeriodProvider({ children }: { children: ReactNode }) {
  // A year by default: monitoring history is measured in months, and a short
  // default makes a quiet site look like an empty one.
  const [window, setWindow] = useState<Window>(() => presetWindow(365));
  const value = useMemo(() => ({ window, setWindow }), [window]);
  return <PeriodContext.Provider value={value}>{children}</PeriodContext.Provider>;
}

export function usePeriod(): Ctx {
  const ctx = useContext(PeriodContext);
  if (!ctx) throw new Error("usePeriod outside PeriodProvider");
  return ctx;
}
