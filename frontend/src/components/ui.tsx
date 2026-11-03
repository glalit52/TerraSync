import clsx from "clsx";
import { AlertTriangle, Inbox, Loader2 } from "lucide-react";
import type { ReactNode } from "react";
import { ApiError } from "../api/client";
import type { ReviewStatus, Severity } from "../api/types";
import { humanise } from "../lib/format";

export function Card({
  title,
  actions,
  children,
  className,
  bodyClass,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClass?: string;
}) {
  return (
    <section className={clsx("card flex min-w-0 flex-col", className)}>
      {(title || actions) && (
        <header className="card-h">
          <h2 className="card-t">{title}</h2>
          <div className="flex items-center gap-2">{actions}</div>
        </header>
      )}
      <div className={clsx("min-h-0 flex-1", bodyClass ?? "p-4")}>{children}</div>
    </section>
  );
}

export function Stat({
  label,
  value,
  sub,
  tone = "ink",
  icon,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: "ink" | "accent" | "crit" | "high" | "med" | "info";
  icon?: ReactNode;
}) {
  const toneClass = {
    ink: "text-ink",
    accent: "text-accent",
    crit: "text-crit",
    high: "text-high",
    med: "text-med",
    info: "text-info",
  }[tone];
  return (
    <div className="card flex items-start gap-3 px-4 py-3">
      {icon && <div className={clsx("mt-0.5 rounded-md bg-raised p-2", toneClass)}>{icon}</div>}
      <div className="min-w-0">
        <div className="card-t">{label}</div>
        <div className={clsx("mt-1 text-2xl font-semibold tabular-nums", toneClass)}>{value}</div>
        {sub && <div className="mt-0.5 text-xs leading-snug text-dim">{sub}</div>}
      </div>
    </div>
  );
}

const SEV: Record<Severity, string> = {
  critical: "border-crit/50 bg-crit/10 text-crit",
  high: "border-high/50 bg-high/10 text-high",
  medium: "border-med/50 bg-med/10 text-med",
  low: "border-low/40 bg-low/10 text-low",
};

export function SeverityBadge({ severity }: { severity: Severity }) {
  return <span className={clsx("chip uppercase tracking-wide", SEV[severity])}>{severity}</span>;
}

export function PriorityBadge({ p }: { p: number }) {
  const cls = p === 1 ? SEV.critical : p === 2 ? SEV.high : p === 3 ? SEV.medium : SEV.low;
  return <span className={clsx("chip font-mono", cls)}>P{p}</span>;
}

const REVIEW: Record<ReviewStatus, string> = {
  pending: "border-line text-dim",
  confirmed: "border-accent/50 bg-accent/10 text-accent",
  rejected: "border-low/40 text-low line-through",
  escalated: "border-high/50 bg-high/10 text-high",
};

export function ReviewBadge({ status }: { status: ReviewStatus }) {
  return <span className={clsx("chip", REVIEW[status])}>{humanise(status)}</span>;
}

export function Tag({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={clsx("chip border-line text-dim", className)}>{children}</span>;
}

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 p-8 text-sm text-dim">
      <Loader2 className="h-4 w-4 animate-spin" /> {label}…
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 p-8 text-center">
      <Inbox className="h-6 w-6 text-dim" />
      <div className="text-sm font-medium">{title}</div>
      {children && <div className="max-w-md text-xs text-dim">{children}</div>}
    </div>
  );
}

export function ErrorState({ error }: { error: unknown }) {
  const e = error as ApiError | Error;
  const status = e instanceof ApiError ? e.status : undefined;
  const hint = e instanceof ApiError ? e.hint : undefined;
  return (
    <div className="m-4 flex gap-3 rounded-md border border-crit/40 bg-crit/5 p-3 text-sm">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-crit" />
      <div>
        <div className="font-medium text-crit">
          {status === 403 ? "Not permitted for your role" : "Request failed"}
          {status ? <span className="ml-2 font-mono text-xs opacity-70">HTTP {status}</span> : null}
        </div>
        <div className="mt-0.5 text-dim">{e?.message}</div>
        {hint && <div className="mt-1 text-xs text-dim">Hint: {hint}</div>}
      </div>
    </div>
  );
}

/** Renders the right thing for a react-query result: spinner, error, or children. */
export function Q<T>({
  q,
  children,
  empty,
}: {
  q: { isLoading: boolean; error: unknown; data: T | undefined };
  children: (data: T) => ReactNode;
  empty?: (data: T) => ReactNode | false;
}) {
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorState error={q.error} />;
  if (q.data === undefined) return null;
  const e = empty?.(q.data);
  if (e) return <>{e}</>;
  return <>{children(q.data)}</>;
}

export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
}: {
  tabs: { id: T; label: ReactNode }[];
  value: T;
  onChange: (t: T) => void;
}) {
  return (
    <div role="tablist" className="flex gap-1 overflow-x-auto border-b border-line">
      {tabs.map((t) => (
        <button
          key={t.id}
          role="tab"
          aria-selected={value === t.id}
          onClick={() => onChange(t.id)}
          className={clsx(
            "-mb-px whitespace-nowrap border-b-2 px-3 py-2 text-sm transition",
            value === t.id ? "border-accent text-ink" : "border-transparent text-dim hover:text-ink",
          )}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function PageHeader({
  title,
  subtitle,
  actions,
  crumbs,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  crumbs?: ReactNode;
}) {
  return (
    <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
      <div className="min-w-0">
        {crumbs && <div className="mb-1 text-xs text-dim">{crumbs}</div>}
        <h1 className="truncate text-xl font-semibold tracking-tight">{title}</h1>
        {subtitle && <p className="mt-0.5 text-sm text-dim">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function KeyValue({ rows }: { rows: [ReactNode, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[minmax(110px,auto)_1fr] gap-x-4 gap-y-1.5 text-sm">
      {rows.map(([k, v], i) => (
        <div key={i} className="contents">
          <dt className="text-dim">{k}</dt>
          <dd className="min-w-0 break-words">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Meter({ value, tone = "accent" }: { value: number; tone?: "accent" | "crit" | "high" | "med" }) {
  const v = Math.max(0, Math.min(1, value));
  const bar = { accent: "bg-accent", crit: "bg-crit", high: "bg-high", med: "bg-med" }[tone];
  return (
    <div className="h-1.5 w-full overflow-hidden rounded bg-raised" aria-valuenow={Math.round(v * 100)} role="meter">
      <div className={clsx("h-full rounded", bar)} style={{ width: `${v * 100}%` }} />
    </div>
  );
}

/** A plain statement of what the system does not claim. Used wherever a number could be over-read. */
export function Caveat({ children }: { children: ReactNode }) {
  return <p className="rounded-md border border-line bg-raised/50 px-3 py-2 text-xs text-dim">{children}</p>;
}
