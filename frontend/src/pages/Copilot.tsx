import { Bot, Send, ShieldAlert, User } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useCopilot, useSites } from "../api/hooks";
import type { CopilotAnswer } from "../api/types";
import { Card, Caveat, ErrorState, PageHeader, Tag } from "../components/ui";
import { humanise } from "../lib/format";

interface Turn {
  q: string;
  a?: CopilotAnswer;
  error?: unknown;
}

function citationLink(kind: string, ref: string): string | null {
  if (kind === "change") return `/changes?focus=${ref}`;
  if (kind === "alert") return `/alerts?focus=${ref}`;
  if (kind === "evidence") return `/evidence/${ref}`;
  if (kind === "coverage" || kind === "site" || kind === "aoi") return `/sites/${ref}`;
  return null;
}

function AnswerView({ a }: { a: CopilotAnswer }) {
  if (a.intent === "refused" || (!a.answer && a.unsupported)) {
    return (
      <div className="flex gap-2 rounded-md border border-med/40 bg-med/5 p-3 text-sm">
        <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-med" />
        <div>
          <div className="font-medium text-med">Declined</div>
          <div className="text-dim">{a.unsupported}</div>
        </div>
      </div>
    );
  }
  const cols = a.rows.length ? Object.keys(a.rows[0]).slice(0, 6) : [];
  return (
    <div className="space-y-3">
      <p className="whitespace-pre-wrap text-sm leading-relaxed">{a.answer}</p>
      {a.unsupported && <Caveat>Not answered: {a.unsupported}</Caveat>}
      {cols.length > 0 && (
        <div className="max-h-64 overflow-auto rounded-md border border-line">
          <table className="tbl">
            <thead>
              <tr>{cols.map((c) => <th key={c}>{humanise(c)}</th>)}</tr>
            </thead>
            <tbody>
              {a.rows.slice(0, 50).map((r, i) => (
                <tr key={i}>{cols.map((c) => <td key={c} className="text-xs">{String(r[c] ?? "")}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {a.citations.length > 0 && (
        <div>
          <div className="card-t mb-1">Citations</div>
          <div className="flex flex-wrap gap-1.5">
            {a.citations.map((c, i) => {
              const to = citationLink(c.kind, c.ref);
              const body = (
                <>
                  <span className="text-accent">[{i + 1}]</span> {c.kind} · <span className="font-mono">{c.ref}</span>
                </>
              );
              return to ? (
                <Link key={i} to={to} title={c.detail} className="chip border-line hover:border-accent/60">{body}</Link>
              ) : (
                <span key={i} title={c.detail} className="chip border-line">{body}</span>
              );
            })}
          </div>
        </div>
      )}
      <div className="text-[11px] text-dim">intent: {a.intent}</div>
    </div>
  );
}

export default function Copilot() {
  const [params] = useSearchParams();
  const ask = useCopilot();
  const sites = useSites();
  const [turns, setTurns] = useState<Turn[]>([]);
  const [q, setQ] = useState(params.get("q") ?? "");
  const end = useRef<HTMLDivElement>(null);

  // A block body on purpose: scrollIntoView returns a Promise in current
  // Chromium, and a concise arrow would hand it to React as the cleanup.
  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns]);

  const send = async (question: string) => {
    if (!question.trim()) return;
    setQ("");
    setTurns((t) => [...t, { q: question }]);
    try {
      const a = await ask.mutateAsync(question);
      setTurns((t) => t.map((x, i) => (i === t.length - 1 ? { ...x, a } : x)));
    } catch (error) {
      setTurns((t) => t.map((x, i) => (i === t.length - 1 ? { ...x, error } : x)));
    }
  };

  const first = sites.data?.[0]?.name.split(" — ")[0] ?? "Mundra";
  const suggestions = [
    `what changed at ${first} in the last 30 days`,
    "which sites have the highest anomaly scores",
    "show significant changes this week",
    "why was the latest alert generated",
    "generate a weekly report",
  ];

  return (
    <div className="mx-auto flex h-[calc(100vh-150px)] max-w-4xl flex-col">
      <PageHeader title="Copilot" subtitle="Questions answered from the record, with citations. The model does language; the store does facts." />
      <Card className="flex-1" bodyClass="p-4 overflow-y-auto">
        {!turns.length && (
          <div className="flex h-full flex-col items-center justify-center gap-4 text-center">
            <Bot className="h-10 w-10 text-accent" />
            <p className="max-w-md text-sm text-dim">
              Ask about changes, anomalies, alerts and coverage. Questions about intent, attribution, threat or targeting are declined by design.
            </p>
            <div className="flex max-w-2xl flex-wrap justify-center gap-2">
              {suggestions.map((s) => (
                <button key={s} className="btn text-xs" onClick={() => send(s)}>{s}</button>
              ))}
            </div>
          </div>
        )}
        <div className="space-y-5">
          {turns.map((t, i) => (
            <div key={i} className="space-y-3">
              <div className="flex gap-3">
                <div className="rounded-md bg-raised p-1.5"><User className="h-4 w-4 text-dim" /></div>
                <div className="pt-1 text-sm font-medium">{t.q}</div>
              </div>
              <div className="flex gap-3">
                <div className="h-fit rounded-md bg-accent/15 p-1.5"><Bot className="h-4 w-4 text-accent" /></div>
                <div className="min-w-0 flex-1">
                  {t.a ? <AnswerView a={t.a} /> : t.error ? <ErrorState error={t.error} /> : <span className="text-sm text-dim">Consulting the record…</span>}
                </div>
              </div>
            </div>
          ))}
          <div ref={end} />
        </div>
      </Card>
      <form
        className="mt-3 flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          send(q);
        }}
      >
        <input className="input py-2" placeholder="Ask about a site, a period, an alert…" value={q} onChange={(e) => setQ(e.target.value)} autoFocus />
        <button className="btn btn-primary px-4" disabled={ask.isPending || !q.trim()}>
          <Send className="h-4 w-4" />
        </button>
      </form>
      <div className="mt-2 flex gap-2 text-[11px] text-dim">
        <Tag>grounded</Tag> <Tag>cited</Tag> <Tag>no intent or attribution</Tag>
      </div>
    </div>
  );
}
