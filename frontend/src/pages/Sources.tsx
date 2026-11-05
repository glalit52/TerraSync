import { CheckCircle2, CircleDashed } from "lucide-react";
import { useProviders } from "../api/hooks";
import { Card, Caveat, PageHeader, Q, Tag } from "../components/ui";

export default function Sources() {
  const p = useProviders();
  return (
    <div className="space-y-4">
      <PageHeader title="Data sources" subtitle="Imagery providers behind one STAC seam. Switching vendor is one environment variable." />
      <Q q={p}>
        {(d) => (
          <>
            <div className="text-sm text-dim">
              Selected: <span className="font-mono text-accent">{d.selected}</span> · change with <span className="font-mono">{d.select_with}</span>
            </div>
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {d.providers.map((x) => (
                <Card
                  key={x.name}
                  title={<span className="normal-case tracking-normal text-ink">{x.title}</span>}
                  actions={x.selected ? <Tag className="text-accent">selected</Tag> : null}
                >
                  <div className="space-y-2 text-sm">
                    <div className="flex items-center gap-2">
                      {x.ready ? <CheckCircle2 className="h-4 w-4 text-accent" /> : <CircleDashed className="h-4 w-4 text-dim" />}
                      <span>{x.ready ? "Configured" : "Not configured"}</span>
                      <span className="ml-auto flex gap-1">
                        <Tag>{x.free ? "free" : "paid"}</Tag>
                        <Tag>{x.needs_account ? "account" : "open"}</Tag>
                      </span>
                    </div>
                    <div className="flex flex-wrap gap-1">{x.constellations.map((c) => <Tag key={c} className="text-info">{c}</Tag>)}</div>
                    {x.missing.length > 0 && (
                      <div className="text-xs text-dim">
                        Set: {x.missing.map((m) => <span key={m} className="mono mr-1 rounded bg-raised px-1">{m}</span>)}
                      </div>
                    )}
                    {x.notes && <p className="text-xs text-dim">{x.notes}</p>}
                  </div>
                </Card>
              ))}
            </div>
          </>
        )}
      </Q>
      <Caveat>Secrets are read from the server's environment only and never returned by the API: this screen shows which variable names are missing, not their values. Configuration is checked without contacting any vendor; run <span className="mono">terrashield providers --check</span> for a live call.</Caveat>
    </div>
  );
}
