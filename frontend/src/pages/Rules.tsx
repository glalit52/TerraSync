import { useRules } from "../api/hooks";
import { Card, Caveat, Empty, PageHeader, Q, SeverityBadge, Tag } from "../components/ui";
import { humanise } from "../lib/format";

export default function Rules() {
  const rules = useRules();
  return (
    <div className="space-y-4">
      <PageHeader title="Alert rules" subtitle="Declarative conditions over findings. A finding raises an alert only when a rule matches it." />
      <Q q={rules} empty={(d) => !d.length && <Empty title="No rules" />}>
        {(d) => (
          <div className="grid gap-3 lg:grid-cols-2">
            {d.map((r) => (
              <Card
                key={r.id}
                title={<span className="normal-case tracking-normal text-ink">{r.name}</span>}
                actions={<>{r.enabled ? <Tag className="text-accent">enabled</Tag> : <Tag>disabled</Tag>}<SeverityBadge severity={r.severity_floor} /></>}
              >
                <div className="space-y-3 text-sm">
                  <p className="rounded bg-raised px-2 py-1.5 font-mono text-[11px] leading-relaxed text-dim">{r.description}</p>
                  <div>
                    <div className="card-t mb-1">Conditions (all must hold)</div>
                    <ul className="space-y-1">
                      {r.conditions.map((c, i) => (
                        <li key={i} className="font-mono text-xs">
                          <span className="text-info">{c.field}</span> <span className="text-dim">{c.op}</span> <span className="text-accent">{JSON.stringify(c.value)}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                  <div className="flex flex-wrap gap-2 text-xs text-dim">
                    <span>Scope: {r.aoi_ids.length ? r.aoi_ids.join(", ") : r.aoi_kinds.length ? r.aoi_kinds.map(humanise).join(", ") : "all sites"}</span>
                    <span>· Channels: {r.channels.join(", ")}</span>
                    <span>· Suppress repeats for {r.suppress_days} d</span>
                  </div>
                </div>
              </Card>
            ))}
          </div>
        )}
      </Q>
      <Caveat>Rule editing is available from the CLI and API today; the in-console rule builder is next on the roadmap.</Caveat>
    </div>
  );
}
