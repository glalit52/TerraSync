import { Copy, FileCheck2 } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { useEvidence, useEvidencePath } from "../api/hooks";
import { Card, Caveat, KeyValue, PageHeader, Q, Tag } from "../components/ui";
import { fmtDateTime, humanise } from "../lib/format";

export default function EvidencePage() {
  const { id } = useParams();
  const ev = useEvidence(id);
  const path = useEvidencePath(ev.data?.finding_id, ev.data?.aoi_id);

  return (
    <Q q={ev}>
      {(e) => {
        const measures = Object.entries(e.measurements ?? {});
        return (
          <div className="space-y-4">
            <PageHeader
              crumbs={<><Link to={`/sites/${e.aoi_id}`} className="hover:underline">{e.aoi_id}</Link> / evidence</>}
              title={<span className="flex items-center gap-2"><FileCheck2 className="h-5 w-5 text-accent" /> Evidence {e.id}</span>}
              subtitle={`${humanise(e.finding_kind)} ${e.finding_id} · finding at ${fmtDateTime(e.finding_at)}`}
              actions={
                <button className="btn" onClick={() => navigator.clipboard?.writeText(e.sha256)}>
                  <Copy className="h-4 w-4" /> Copy sha256
                </button>
              }
            />
            <div className="grid gap-4 xl:grid-cols-[1.4fr_1fr]">
              <div className="space-y-4">
                <Card title="Narrative">
                  <p className="text-sm leading-relaxed">{e.narrative}</p>
                </Card>
                <Card title={`Artefacts (${e.artefacts.length})`} bodyClass="p-0">
                  <div className="overflow-x-auto">
                    <table className="tbl">
                      <thead>
                        <tr>
                          <th>Kind</th>
                          <th>Reference</th>
                          <th>Note</th>
                          <th>sha256</th>
                        </tr>
                      </thead>
                      <tbody>
                        {e.artefacts.map((a, i) => (
                          <tr key={i}>
                            <td><Tag>{a.kind}</Tag></td>
                            <td className="mono break-all">{a.ref}</td>
                            <td className="text-xs text-dim">{a.note}</td>
                            <td className="mono text-dim" title={a.sha256}>{a.sha256.slice(0, 12)}…</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </Card>
                {e.gaps?.length ? (
                  <Card title="What could not be established">
                    <ul className="list-disc space-y-1 pl-5 text-sm text-dim">
                      {e.gaps.map((g) => <li key={g}>{g}</li>)}
                    </ul>
                  </Card>
                ) : null}
              </div>
              <div className="space-y-4">
                <Card title="Integrity">
                  <KeyValue
                    rows={[
                      ["Bundle hash", <span key="h" className="mono break-all">{e.sha256}</span>],
                      ["Created", fmtDateTime(e.created_at)],
                      ["Finding", <span key="f" className="mono">{e.finding_id}</span>],
                    ]}
                  />
                  <p className="mt-3 text-xs text-dim">Evidence packs are hash-chained and verifiable offline with <span className="mono">terrashield evidence</span>.</p>
                </Card>
                {measures.length > 0 && (
                  <Card title="Measurements" bodyClass="p-0">
                    <table className="tbl">
                      <tbody>
                        {measures.map(([k, v]) => (
                          <tr key={k}>
                            <td className="text-dim">{humanise(k)}</td>
                            <td className="text-right font-mono">{typeof v === "number" ? v.toLocaleString(undefined, { maximumFractionDigits: 4 }) : String(v)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </Card>
                )}
                <Card title="Evidence path">
                  {path.data?.path?.length ? (
                    <ol className="relative space-y-3 border-l border-line pl-4">
                      {path.data.path.map((n) => (
                        <li key={n.id}>
                          <span className="absolute -left-[5px] mt-1.5 h-2.5 w-2.5 rounded-full bg-accent" />
                          <div className="text-xs uppercase tracking-wider text-dim">{n.kind}</div>
                          <div className="mono break-all">{n.label}</div>
                        </li>
                      ))}
                    </ol>
                  ) : (
                    <p className="text-sm text-dim">
                      {path.error ? "This finding is outside the graph's window (the last 365 days)." : "Walking back from the finding…"}
                    </p>
                  )}
                </Card>
                <Caveat>Evidence records what was measured and what was not. It never records intent or attribution; the schema has no field for either.</Caveat>
              </div>
            </div>
          </div>
        );
      }}
    </Q>
  );
}
