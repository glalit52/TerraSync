import { useState } from "react";
import { useReports } from "../api/hooks";
import { Card, Empty, PageHeader, Q } from "../components/ui";
import { fmtDateTime, humanise } from "../lib/format";

export default function Reports() {
  const reports = useReports();
  const [open, setOpen] = useState<number | null>(null);
  return (
    <div className="space-y-4">
      <PageHeader title="Reports" subtitle="Daily, weekly and site reports, each line traceable to findings." />
      <Q
        q={reports}
        empty={(d) =>
          !d.length && (
            <Card>
              <Empty title="No reports have been generated yet">
                Generate one with <span className="mono">terrashield report daily|weekly|site</span>, or ask the copilot for a weekly report.
              </Empty>
            </Card>
          )
        }
      >
        {(d) => (
          <div className="space-y-2">
            {d.map((r, i) => (
              <Card
                key={String(r.id ?? i)}
                title={<button onClick={() => setOpen(open === i ? null : i)} className="text-left normal-case tracking-normal text-ink">{String(r.title ?? humanise(String(r.kind ?? "report")))}</button>}
                actions={<span className="text-xs text-dim">{fmtDateTime(String(r.created_at ?? ""))}</span>}
              >
                {open === i ? <pre className="whitespace-pre-wrap font-mono text-xs leading-relaxed">{typeof r.body === "string" ? r.body : JSON.stringify(r, null, 2)}</pre> : <span className="text-xs text-dim">Click the title to expand.</span>}
              </Card>
            ))}
          </div>
        )}
      </Q>
    </div>
  );
}
