import { Plus } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { useCreateWatchlist, useSites, useWatchlists } from "../api/hooks";
import { Card, Empty, ErrorState, PageHeader, Q, SeverityBadge } from "../components/ui";
import { humanise } from "../lib/format";
import { useAuth } from "../state/auth";

export default function Watchlists() {
  const wl = useWatchlists();
  const sites = useSites();
  const create = useCreateWatchlist();
  const { can } = useAuth();
  const [name, setName] = useState("");
  const [priority, setPriority] = useState("medium");
  const [chosen, setChosen] = useState<string[]>([]);
  const siteName = (id: string) => sites.data?.find((s) => s.id === id)?.name ?? id;

  return (
    <div className="space-y-4">
      <PageHeader title="Watchlists" subtitle="Named groups of sites, checked together and briefed together." />
      <div className="grid gap-4 xl:grid-cols-[1.4fr_1fr]">
        <Q q={wl} empty={(d) => !d.length && <Card><Empty title="No watchlists yet">Group sites that are briefed together, such as “Western ports” or “Energy assets”.</Empty></Card>}>
          {(d) => (
            <div className="grid gap-3 md:grid-cols-2">
              {d.map((w) => (
                <Card key={w.id} title={w.name} actions={<SeverityBadge severity={w.priority} />}>
                  <ul className="space-y-1 text-sm">
                    {w.aoi_ids.map((id) => (
                      <li key={id}><Link className="text-accent hover:underline" to={`/sites/${id}`}>{siteName(id)}</Link></li>
                    ))}
                  </ul>
                </Card>
              ))}
            </div>
          )}
        </Q>
        {can("watchlist.manage") && (
          <Card title="New watchlist">
            <div className="space-y-3">
              <input className="input" placeholder="Name" value={name} onChange={(e) => setName(e.target.value)} />
              <select className="input" value={priority} onChange={(e) => setPriority(e.target.value)}>
                {["low", "medium", "high", "critical"].map((p) => <option key={p} value={p}>{humanise(p)} priority</option>)}
              </select>
              <div className="max-h-56 space-y-1 overflow-y-auto rounded-md border border-line p-2">
                {sites.data?.map((s) => (
                  <label key={s.id} className="flex items-center gap-2 text-sm">
                    <input type="checkbox" checked={chosen.includes(s.id)} onChange={(e) => setChosen((c) => (e.target.checked ? [...c, s.id] : c.filter((x) => x !== s.id)))} />
                    {s.name}
                  </label>
                ))}
              </div>
              {create.error ? <ErrorState error={create.error} /> : null}
              <button
                className="btn btn-primary"
                disabled={!name || !chosen.length || create.isPending}
                onClick={() => create.mutate({ name, priority, aoi_ids: chosen }, { onSuccess: () => (setName(""), setChosen([])) })}
              >
                <Plus className="h-4 w-4" /> Create
              </button>
            </div>
          </Card>
        )}
      </div>
    </div>
  );
}
