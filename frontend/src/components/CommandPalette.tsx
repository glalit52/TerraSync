import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAlerts, useSites } from "../api/hooks";
import { NAV } from "./Layout";

interface Item {
  id: string;
  label: string;
  hint: string;
  to: string;
  text: string;
}

export function CommandPalette({ onClose }: { onClose: () => void }) {
  const nav = useNavigate();
  const sites = useSites();
  const alerts = useAlerts({ maxPriority: 4 });
  const [q, setQ] = useState("");
  const [i, setI] = useState(0);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    input.current?.focus();
  }, []);

  const items = useMemo<Item[]>(() => {
    const all: Item[] = [
      ...NAV.map((n) => ({ id: `nav:${n.to}`, label: n.label, hint: "Page", to: n.to, text: `${n.label} ${n.keywords ?? ""}` })),
      ...(sites.data ?? []).map((s) => ({
        id: `site:${s.id}`,
        label: s.name,
        hint: `Site · ${s.id}`,
        to: `/sites/${s.id}`,
        text: `${s.name} ${s.id} ${s.kind}`,
      })),
      ...(alerts.data?.alerts ?? []).slice(0, 60).map((a) => ({
        id: `alert:${a.id}`,
        label: a.title,
        hint: `P${a.priority} · ${a.aoi_id}`,
        to: `/alerts?focus=${a.id}`,
        text: `${a.title} ${a.aoi_id} ${a.id}`,
      })),
    ];
    const terms = q.toLowerCase().split(/\s+/).filter(Boolean);
    return all.filter((it) => terms.every((t) => it.text.toLowerCase().includes(t))).slice(0, 12);
  }, [q, sites.data, alerts.data]);

  const go = (it: Item | undefined) => {
    if (!it) return;
    nav(it.to);
    onClose();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/50 p-4 pt-[12vh]" onClick={onClose}>
      <div className="card w-full max-w-xl overflow-hidden shadow-2xl" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Command palette">
        <input
          ref={input}
          className="w-full border-b border-line bg-transparent px-4 py-3 text-sm outline-none"
          placeholder="Search pages, sites, alerts…"
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setI(0);
          }}
          onKeyDown={(e) => {
            if (e.key === "Escape") onClose();
            if (e.key === "ArrowDown") (e.preventDefault(), setI((x) => Math.min(items.length - 1, x + 1)));
            if (e.key === "ArrowUp") (e.preventDefault(), setI((x) => Math.max(0, x - 1)));
            if (e.key === "Enter") go(items[i]);
          }}
        />
        <ul className="max-h-[50vh] overflow-y-auto py-1">
          {items.map((it, k) => (
            <li key={it.id}>
              <button
                className={`flex w-full items-center justify-between gap-3 px-4 py-2 text-left text-sm ${k === i ? "bg-raised" : ""}`}
                onMouseEnter={() => setI(k)}
                onClick={() => go(it)}
              >
                <span className="truncate">{it.label}</span>
                <span className="shrink-0 text-xs text-dim">{it.hint}</span>
              </button>
            </li>
          ))}
          {!items.length && <li className="px-4 py-6 text-center text-sm text-dim">Nothing matches.</li>}
        </ul>
        <div className="flex gap-3 border-t border-line px-4 py-2 text-[11px] text-dim">
          <span><span className="kbd">↑↓</span> move</span>
          <span><span className="kbd">Enter</span> open</span>
          <span><span className="kbd">Esc</span> close</span>
          <span className="ml-auto"><span className="kbd">g</span> then <span className="kbd">a</span> alerts · <span className="kbd">m</span> map · <span className="kbd">o</span> orbit</span>
        </div>
      </div>
    </div>
  );
}
