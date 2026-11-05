import clsx from "clsx";
import {
  Activity,
  Anchor,
  Bell,
  Building2,
  BookOpen,
  Bot,
  CalendarRange,
  Database,
  Eye,
  FileText,
  Globe2,
  LayoutDashboard,
  LogOut,
  MapPin,
  Moon,
  Orbit,
  Plug,
  Radar,
  Search,
  Shield,
  ShieldCheck,
  SlidersHorizontal,
  Sun,
  Telescope,
  Waypoints,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useAlerts, useHealth, useTrackAlerts } from "../api/hooks";
import { useAuth } from "../state/auth";
import { PRESETS, presetWindow, usePeriod } from "../state/period";
import { CommandPalette } from "./CommandPalette";

export interface NavItem {
  to: string;
  label: string;
  icon: ReactNode;
  group: string;
  keywords?: string;
}

export const NAV: NavItem[] = [
  { to: "/", label: "Command", icon: <LayoutDashboard />, group: "Operate", keywords: "overview home dashboard" },
  { to: "/map", label: "Global map", icon: <Globe2 />, group: "Operate", keywords: "map geo layers" },
  { to: "/alerts", label: "Alert queue", icon: <Bell />, group: "Operate", keywords: "triage review queue" },
  { to: "/live", label: "Live tracks", icon: <Radar />, group: "Operate", keywords: "radar sensors tracks geofence intrusion border live" },
  { to: "/copilot", label: "Copilot", icon: <Bot />, group: "Operate", keywords: "ask question ai" },
  { to: "/sites", label: "Sites", icon: <MapPin />, group: "Intelligence", keywords: "aoi areas monitored" },
  { to: "/changes", label: "Change explorer", icon: <Activity />, group: "Intelligence", keywords: "change detection findings" },
  { to: "/assets", label: "Asset intelligence", icon: <Building2 />, group: "Intelligence", keywords: "property portfolio insurance exposure footprint disaster location report" },
  { to: "/maritime", label: "Maritime", icon: <Anchor />, group: "Intelligence", keywords: "vessels ais ports ships" },
  { to: "/orbit", label: "Orbit (SDA)", icon: <Orbit />, group: "Intelligence", keywords: "space satellites tle overflight sda" },
  { to: "/graph", label: "Evidence graph", icon: <Waypoints />, group: "Intelligence", keywords: "knowledge graph provenance" },
  { to: "/watchlists", label: "Watchlists", icon: <Eye />, group: "Configure", keywords: "watch" },
  { to: "/rules", label: "Alert rules", icon: <SlidersHorizontal />, group: "Configure", keywords: "rules conditions" },
  { to: "/reports", label: "Reports", icon: <FileText />, group: "Configure", keywords: "brief daily weekly" },
  { to: "/sensing", label: "Sensing limits", icon: <Telescope />, group: "Configure", keywords: "resolution gsd platforms" },
  { to: "/sources", label: "Data sources", icon: <Database />, group: "Platform", keywords: "providers imagery stac" },
  { to: "/integrations", label: "Integrations", icon: <Plug />, group: "Platform", keywords: "api webhook sdk" },
  { to: "/admin", label: "Admin & audit", icon: <ShieldCheck />, group: "Platform", keywords: "audit health permissions" },
];

function useTheme(): [boolean, () => void] {
  const [dark, setDark] = useState(() => {
    try {
      return localStorage.getItem("terrashield.theme") !== "light";
    } catch {
      return true;
    }
  });
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    try {
      localStorage.setItem("terrashield.theme", dark ? "dark" : "light");
    } catch {
      /* per-viewer convenience only */
    }
  }, [dark]);
  return [dark, () => setDark((d) => !d)];
}

function PeriodPicker() {
  const { window, setWindow } = usePeriod();
  const [open, setOpen] = useState(false);
  const [from, setFrom] = useState(window.from);
  const [to, setTo] = useState(window.to);
  useEffect(() => {
    setFrom(window.from);
    setTo(window.to);
  }, [window]);
  return (
    <div className="relative">
      <button className="btn" onClick={() => setOpen((o) => !o)} aria-haspopup="dialog">
        <CalendarRange className="h-4 w-4 text-dim" />
        <span className="hidden sm:inline">{window.label}</span>
        <span className="font-mono text-xs text-dim">
          {window.from} → {window.to}
        </span>
      </button>
      {open && (
        <div className="card absolute right-0 z-40 mt-2 w-72 p-3 shadow-xl" role="dialog">
          <div className="card-t mb-2">Analysis window</div>
          <div className="mb-3 grid grid-cols-4 gap-1">
            {PRESETS.map((d) => (
              <button
                key={d}
                className="btn justify-center px-1 text-xs"
                onClick={() => {
                  setWindow(presetWindow(d));
                  setOpen(false);
                }}
              >
                {d}d
              </button>
            ))}
          </div>
          <div className="grid grid-cols-2 gap-2">
            <label>
              <span className="label">From</span>
              <input type="date" className="input" value={from} onChange={(e) => setFrom(e.target.value)} />
            </label>
            <label>
              <span className="label">To</span>
              <input type="date" className="input" value={to} onChange={(e) => setTo(e.target.value)} />
            </label>
          </div>
          <button
            className="btn btn-primary mt-3 w-full justify-center"
            disabled={!from || !to || from > to}
            onClick={() => {
              setWindow({ from, to, label: "Custom" });
              setOpen(false);
            }}
          >
            Apply
          </button>
          <p className="mt-2 text-[11px] text-dim">Every screen reads this window, so numbers always describe the same dates.</p>
        </div>
      )}
    </div>
  );
}

export function Layout() {
  const { me, logout } = useAuth();
  const [dark, toggleTheme] = useTheme();
  const [palette, setPalette] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const health = useHealth();
  const pending = useAlerts({ status: "pending", maxPriority: 2 });
  const trackAlerts = useTrackAlerts("pending");
  const nav = useNavigate();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPalette((p) => !p);
      }
      if (e.key === "g" && !(e.target instanceof HTMLInputElement) && !(e.target instanceof HTMLTextAreaElement)) {
        const next = (ev: KeyboardEvent) => {
          const map: Record<string, string> = { c: "/", m: "/map", a: "/alerts", s: "/sites", o: "/orbit", q: "/copilot" };
          if (map[ev.key]) nav(map[ev.key]);
          window.removeEventListener("keydown", next);
        };
        window.addEventListener("keydown", next, { once: true });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [nav]);

  const groups = [...new Set(NAV.map((n) => n.group))];
  const p12 = pending.data ? pending.data.queue.priority_1 + pending.data.queue.priority_2 : 0;

  return (
    <div className="flex h-full flex-col">
      {/* Classification banner: set by the deployment. The demo estate is
          modelled imagery over civilian sites, and the banner says so. */}
      <div className="flex h-6 shrink-0 items-center print:hidden justify-center gap-2 bg-accent/90 text-[11px] font-bold uppercase tracking-[0.2em] text-black">
        <Shield className="h-3 w-3" /> {import.meta.env.VITE_CLASSIFICATION ?? "Unclassified // Demo estate — modelled imagery"}
      </div>
      <div className="flex min-h-0 flex-1">
        <aside className={clsx("flex shrink-0 print:hidden flex-col border-r border-line bg-panel transition-all", collapsed ? "w-14" : "w-56")}>
          <button className="flex h-14 items-center gap-2 border-b border-line px-3 text-left" onClick={() => setCollapsed((c) => !c)} title="Collapse sidebar">
            <svg viewBox="0 0 32 32" className="h-7 w-7 shrink-0">
              <path d="M16 2 4 7v8c0 7.5 5.1 13.4 12 15 6.9-1.6 12-7.5 12-15V7z" fill="none" stroke="rgb(var(--accent))" strokeWidth="2" />
              <circle cx="16" cy="15" r="4" fill="none" stroke="rgb(var(--accent))" strokeWidth="2" />
            </svg>
            {!collapsed && (
              <div className="leading-tight">
                <div className="text-sm font-bold tracking-tight">
                  Terra<span className="text-accent">Shield</span>
                </div>
                <div className="text-[10px] uppercase tracking-widest text-dim">Intelligence console</div>
              </div>
            )}
          </button>
          <nav className="flex-1 overflow-y-auto py-2">
            {groups.map((g) => (
              <div key={g} className="mb-2">
                {!collapsed && <div className="px-4 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-[0.16em] text-dim/70">{g}</div>}
                {NAV.filter((n) => n.group === g).map((n) => (
                  <NavLink
                    key={n.to}
                    to={n.to}
                    end={n.to === "/"}
                    title={n.label}
                    className={({ isActive }) =>
                      clsx(
                        "mx-2 flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-sm transition [&>svg]:h-4 [&>svg]:w-4 [&>svg]:shrink-0",
                        isActive ? "bg-accent/12 text-accent" : "text-dim hover:bg-raised hover:text-ink",
                      )
                    }
                  >
                    {n.icon}
                    {!collapsed && <span className="flex-1 truncate">{n.label}</span>}
                    {!collapsed && n.to === "/alerts" && p12 > 0 && (
                      <span className="rounded bg-crit/20 px-1.5 font-mono text-[10px] text-crit">{p12}</span>
                    )}
                    {!collapsed && n.to === "/live" && (trackAlerts.data?.length ?? 0) > 0 && (
                      <span className="rounded bg-crit/20 px-1.5 font-mono text-[10px] text-crit">{trackAlerts.data!.length}</span>
                    )}
                  </NavLink>
                ))}
              </div>
            ))}
          </nav>
          <div className="border-t border-line p-2">
            <div className={clsx("flex items-center gap-2 rounded-md px-2 py-1.5 text-xs", collapsed && "justify-center")}>
              <span
                className={clsx("h-2 w-2 rounded-full", health.data?.audit_chain_intact ? "bg-accent" : health.error ? "bg-crit" : "bg-med")}
                title="Server and audit chain"
              />
              {!collapsed && (
                <span className="truncate text-dim">
                  {health.data ? (health.data.audit_chain_intact ? "Audit chain intact" : "Audit chain BROKEN") : "Checking server…"}
                </span>
              )}
            </div>
          </div>
        </aside>

        <div className="flex min-w-0 flex-1 flex-col">
          <header className="flex h-14 shrink-0 print:hidden items-center gap-3 border-b border-line bg-panel/60 px-4 backdrop-blur">
            <button
              className="flex w-full max-w-md items-center gap-2 rounded-md border border-line bg-bg px-3 py-1.5 text-sm text-dim hover:border-accent/50"
              onClick={() => setPalette(true)}
            >
              <Search className="h-4 w-4" />
              <span className="flex-1 text-left">Jump to a site, alert or page…</span>
              <span className="kbd">Ctrl K</span>
            </button>
            <div className="ml-auto flex items-center gap-2">
              <PeriodPicker />
              <button className="btn btn-ghost" onClick={toggleTheme} title="Toggle theme" aria-label="Toggle theme">
                {dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
              </button>
              <a className="btn btn-ghost hidden md:inline-flex" href="/console/integrations" onClick={(e) => (e.preventDefault(), nav("/integrations"))}>
                <BookOpen className="h-4 w-4" /> API
              </a>
              <div className="hidden text-right leading-tight lg:block">
                <div className="max-w-[180px] truncate text-xs">{me?.actor}</div>
                <div className="text-[10px] uppercase tracking-wider text-accent">{me?.role}</div>
              </div>
              <button className="btn btn-ghost" onClick={logout} title="Sign out" aria-label="Sign out">
                <LogOut className="h-4 w-4" />
              </button>
            </div>
          </header>
          <main className="min-h-0 flex-1 overflow-y-auto p-4 lg:p-6">
            <Outlet />
          </main>
        </div>
      </div>
      {palette && <CommandPalette onClose={() => setPalette(false)} />}
    </div>
  );
}
