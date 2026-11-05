import { PenLine, Plus, Undo2, X } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useCreateSite, useSites } from "../api/hooks";
import type { LonLat } from "../api/types";
import { WorldMap } from "../components/WorldMap";
import { Card, ErrorState, PageHeader, Q, Tag } from "../components/ui";
import { fmtNum, humanise } from "../lib/format";
import { useAuth } from "../state/auth";

const KINDS = ["port", "airfield", "border_sector", "energy", "industrial", "dam", "transport", "generic"];

function NewSite({ onClose }: { onClose: () => void }) {
  const create = useCreateSite();
  const nav = useNavigate();
  const sites = useSites();
  const [name, setName] = useState("");
  const [kind, setKind] = useState("generic");
  const [country, setCountry] = useState("IN");
  const [description, setDescription] = useState("");
  const [ring, setRing] = useState<LonLat[]>([]);
  const [geojson, setGeojson] = useState("");
  const [mode, setMode] = useState<"draw" | "paste">("draw");
  const [parseErr, setParseErr] = useState("");

  const submit = async () => {
    let boundary: unknown = ring;
    if (mode === "paste") {
      try {
        boundary = JSON.parse(geojson);
      } catch (e) {
        setParseErr(`Not valid JSON: ${(e as Error).message}`);
        return;
      }
    }
    setParseErr("");
    const r = await create.mutateAsync({ name, kind, country, description, boundary });
    nav(`/sites/${r.site.id}`);
  };

  const ok = name && (mode === "draw" ? ring.length >= 3 : geojson.trim());

  return (
    <Card
      title="New monitored site"
      actions={
        <button className="btn btn-ghost" onClick={onClose} aria-label="Close">
          <X className="h-4 w-4" />
        </button>
      }
    >
      <div className="grid gap-4 lg:grid-cols-[320px_1fr]">
        <div className="space-y-3">
          <label className="block">
            <span className="label">Name</span>
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Kandla Port — oil jetty" />
          </label>
          <div className="grid grid-cols-2 gap-2">
            <label>
              <span className="label">Kind</span>
              <select className="input" value={kind} onChange={(e) => setKind(e.target.value)}>
                {KINDS.map((k) => (
                  <option key={k} value={k}>
                    {humanise(k)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span className="label">Country</span>
              <input className="input" value={country} onChange={(e) => setCountry(e.target.value)} maxLength={2} />
            </label>
          </div>
          <label className="block">
            <span className="label">Description</span>
            <textarea className="input h-16 resize-none" value={description} onChange={(e) => setDescription(e.target.value)} />
          </label>
          <div className="flex gap-1 rounded-md border border-line p-1 text-xs">
            {(["draw", "paste"] as const).map((m) => (
              <button key={m} onClick={() => setMode(m)} className={`flex-1 rounded px-2 py-1 ${mode === m ? "bg-raised" : "text-dim"}`}>
                {m === "draw" ? "Draw on map" : "Paste GeoJSON / KML-converted"}
              </button>
            ))}
          </div>
          {mode === "draw" ? (
            <div className="space-y-2 text-xs text-dim">
              <p>Click the map to place vertices. Scroll to zoom, drag to pan. {ring.length} point(s) placed.</p>
              <div className="flex gap-2">
                <button className="btn" onClick={() => setRing((r) => r.slice(0, -1))} disabled={!ring.length}>
                  <Undo2 className="h-4 w-4" /> Undo
                </button>
                <button className="btn" onClick={() => setRing([])} disabled={!ring.length}>
                  Clear
                </button>
              </div>
            </div>
          ) : (
            <textarea
              className="input h-40 font-mono text-xs"
              placeholder='{"type":"Polygon","coordinates":[[[69.70,22.74],[69.73,22.74],[69.73,22.76],[69.70,22.76],[69.70,22.74]]]}'
              value={geojson}
              onChange={(e) => setGeojson(e.target.value)}
            />
          )}
          {parseErr && <p className="text-xs text-crit">{parseErr}</p>}
          {create.error ? <ErrorState error={create.error} /> : null}
          <button className="btn btn-primary w-full justify-center" disabled={!ok || create.isPending} onClick={submit}>
            <Plus className="h-4 w-4" /> Start monitoring
          </button>
          <p className="text-[11px] text-dim">
            The server validates the polygon (closure, self-intersection, size) and says what is wrong if it refuses it. Imagery is backfilled by the
            monitoring job, not by this form.
          </p>
        </div>
        <WorldMap
          height={460}
          drawing={mode === "draw"}
          onDrawPoint={(p) => setRing((r) => [...r, p])}
          polygons={ring.length >= 3 ? [{ id: "draft", ring, tone: "draft" }] : []}
          points={[
            ...(sites.data ?? []).map((s) => ({ id: s.id, lon: s.centroid[0], lat: s.centroid[1], tone: "site" as const, label: s.name.split(" — ")[0] })),
            ...ring.map((p, i) => ({ id: `v${i}`, lon: p[0], lat: p[1], tone: "object" as const, size: 4 })),
          ]}
        />
      </div>
    </Card>
  );
}

export default function Sites() {
  const sites = useSites();
  const nav = useNavigate();
  const { can } = useAuth();
  const [creating, setCreating] = useState(false);
  const [filter, setFilter] = useState("");

  return (
    <div className="space-y-4">
      <PageHeader
        title="Monitored sites"
        subtitle="Each site has its own baseline, history and fingerprint. A changed boundary starts a new baseline."
        actions={
          can("aoi.create") && !creating ? (
            <button className="btn btn-primary" onClick={() => setCreating(true)}>
              <PenLine className="h-4 w-4" /> New site
            </button>
          ) : null
        }
      />
      {creating && <NewSite onClose={() => setCreating(false)} />}
      <input className="input max-w-sm" placeholder="Filter by name, id or kind" value={filter} onChange={(e) => setFilter(e.target.value)} />
      <Q q={sites}>
        {(list) => (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            {list
              .filter((s) => `${s.name} ${s.id} ${s.kind}`.toLowerCase().includes(filter.toLowerCase()))
              .map((s) => (
                <button key={s.id} className="card p-4 text-left transition hover:border-accent/60" onClick={() => nav(`/sites/${s.id}`)}>
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="truncate font-medium">{s.name}</div>
                      <div className="mono text-dim">{s.id}</div>
                    </div>
                    <Tag>{humanise(s.kind)}</Tag>
                  </div>
                  <p className="mt-2 line-clamp-2 text-xs text-dim">{s.description}</p>
                  <div className="mt-3 flex gap-4 text-xs text-dim">
                    <span>{fmtNum(s.area_km2, 2)} km²</span>
                    <span>{s.country}</span>
                    <span className="font-mono">fp {s.fingerprint.slice(0, 8)}</span>
                  </div>
                </button>
              ))}
          </div>
        )}
      </Q>
    </div>
  );
}
