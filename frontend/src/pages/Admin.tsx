import { ShieldAlert, ShieldCheck } from "lucide-react";
import { useAudit, useHealth } from "../api/hooks";
import { Card, Caveat, KeyValue, PageHeader, Q, Stat, Tag } from "../components/ui";
import { fmtDateTime, fmtNum, humanise } from "../lib/format";
import { useAuth } from "../state/auth";

const ROLE_ORDER = ["viewer", "analyst", "supervisor", "admin"];

export default function Admin() {
  const { me, can } = useAuth();
  const health = useHealth();
  const audit = useAudit(can("audit.read"));

  return (
    <div className="space-y-4">
      <PageHeader title="Admin & audit" subtitle="Who you are, what you may do, and the tamper-evident record of what everyone did." />
      <div className="grid gap-4 xl:grid-cols-3">
        <Card title="Your session">
          <KeyValue
            rows={[
              ["Actor", me?.actor],
              ["Organisation", <span key="o" className="mono">{me?.organisation}</span>],
              ["Role", <Tag key="r" className="text-accent">{me?.role}</Tag>],
            ]}
          />
          <div className="mt-3 flex gap-1">
            {ROLE_ORDER.map((r) => (
              <div key={r} className={`h-1.5 flex-1 rounded ${ROLE_ORDER.indexOf(r) <= ROLE_ORDER.indexOf(me?.role ?? "viewer") ? "bg-accent" : "bg-raised"}`} title={r} />
            ))}
          </div>
          <p className="mt-1 text-[11px] text-dim">viewer → analyst → supervisor → admin</p>
        </Card>
        <Card title={`Permissions (${me?.permissions.length ?? 0})`} className="xl:col-span-2">
          <div className="flex flex-wrap gap-1.5">
            {me?.permissions.map((p) => <Tag key={p} className="font-mono">{p}</Tag>)}
          </div>
        </Card>
      </div>

      <Q q={health}>
        {(h) => (
          <>
            <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
              {Object.entries(h.counts).slice(0, 10).map(([k, v]) => <Stat key={k} label={humanise(k)} value={fmtNum(v)} />)}
            </div>
            <Card title="Server">
              <KeyValue
                rows={[
                  ["Status", h.status],
                  ["Schema version", h.schema_version],
                  ["Database", <span key="d" className="mono">{h.database}</span>],
                  [
                    "Audit chain",
                    h.audit_chain_intact ? (
                      <span key="a" className="inline-flex items-center gap-1 text-accent"><ShieldCheck className="h-4 w-4" /> intact</span>
                    ) : (
                      <span key="a" className="inline-flex items-center gap-1 text-crit"><ShieldAlert className="h-4 w-4" /> broken at entry {h.first_bad_audit_entry}</span>
                    ),
                  ],
                ]}
              />
            </Card>
          </>
        )}
      </Q>

      <Card title="Audit log" bodyClass="p-0">
        {!can("audit.read") ? (
          <div className="p-4"><Caveat>Reading the audit log needs the admin role. Every login, image access, review, download, API call and copilot question is recorded there.</Caveat></div>
        ) : (
          <Q q={audit}>
            {(a) => (
              <>
                <div className="border-b border-line px-4 py-2 text-xs">
                  {a.chain_intact ? <span className="text-accent">Hash chain verified end to end.</span> : <span className="text-crit">Chain broken at entry {a.first_bad_entry}.</span>}
                </div>
                <div className="max-h-[480px] overflow-auto">
                  <table className="tbl">
                    <thead>
                      <tr>
                        <th>#</th>
                        <th>When</th>
                        <th>Actor</th>
                        <th>Action</th>
                        <th>Target</th>
                      </tr>
                    </thead>
                    <tbody>
                      {a.entries.map((e, i) => (
                        <tr key={i}>
                          <td className="mono text-dim">{String(e.seq ?? e.id ?? i)}</td>
                          <td className="whitespace-nowrap">{fmtDateTime(String(e.at ?? e.ts ?? e.created_at ?? ""))}</td>
                          <td>{String(e.actor ?? "")}</td>
                          <td className="mono">{String(e.action ?? "")}</td>
                          <td className="mono text-dim">{String(e.target ?? e.resource ?? "")}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </Q>
        )}
      </Card>
    </div>
  );
}
