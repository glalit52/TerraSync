import { Copy, Play } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import { Card, Caveat, PageHeader, Tabs, Tag } from "../components/ui";
import { getToken } from "../api/client";

interface RouteDoc {
  method: "GET" | "POST";
  path: string;
  permission: string;
  what: string;
  example?: string;
}

// The HTTP contract, as served by `terrashield serve`. Kept beside the console
// so an integrator sees exactly what the console itself calls.
const ROUTES: RouteDoc[] = [
  { method: "POST", path: "/api/auth/login", permission: "public", what: "Exchange email + password for a session token" },
  { method: "POST", path: "/api/auth/signup", permission: "public, gated", what: "Create an organisation (only with --allow-signup)" },
  { method: "POST", path: "/api/auth/logout", permission: "public", what: "Revoke the presented token" },
  { method: "GET", path: "/api/me", permission: "any", what: "Actor, organisation, role and permissions", example: "/api/me" },
  { method: "GET", path: "/api/health", permission: "any", what: "Counts and audit-chain integrity", example: "/api/health" },
  { method: "GET", path: "/api/sites", permission: "site.read", what: "Monitored areas", example: "/api/sites" },
  { method: "POST", path: "/api/sites", permission: "aoi.create", what: "Add an area (GeoJSON or [lon,lat] ring)" },
  { method: "GET", path: "/api/sites/{id}", permission: "site.read", what: "Site summary for a period (?from&to)" },
  { method: "GET", path: "/api/sites/{id}/events", permission: "site.read", what: "Findings fused into episodes, with queue reduction" },
  { method: "GET", path: "/api/sites/{id}/observations", permission: "site.read", what: "Measured series behind pattern-of-life" },
  { method: "GET", path: "/api/sites/{id}/projection", permission: "site.read", what: "Trend projection or the reason it is refused (?metric&to)" },
  { method: "GET", path: "/api/sites/{id}/graph", permission: "site.read", what: "Graph stats, or subgraph with ?node&depth" },
  { method: "GET", path: "/api/sites/{id}/fields", permission: "site.read", what: "Named sub-areas" },
  { method: "POST", path: "/api/sites/{id}/fields", permission: "field.create", what: "Add a field" },
  { method: "POST", path: "/api/fields/{id}/retire", permission: "field.update", what: "Retire a field (idempotent)" },
  { method: "GET", path: "/api/fields/at", permission: "site.read", what: "Which field a coordinate falls in (?lon&lat)" },
  { method: "GET", path: "/api/imagery", permission: "imagery.read", what: "Scenes for a site, including rejected ones (?site)" },
  { method: "GET", path: "/api/changes", permission: "site.read", what: "Change events (?site&from&to&limit≤5000), with a truncated flag", example: "/api/changes" },
  { method: "GET", path: "/api/objects", permission: "site.read", what: "Detections (?site&scene)" },
  { method: "GET", path: "/api/timeline", permission: "site.read", what: "Scenes, changes and anomaly scores on one axis (?site)" },
  { method: "GET", path: "/api/alerts", permission: "alert.read", what: "Ranked queue (?site&status&max_priority)", example: "/api/alerts?max_priority=2" },
  { method: "POST", path: "/api/reviews/{id}", permission: "finding.review", what: "Confirm / reject / escalate an alert or change" },
  { method: "GET", path: "/api/evidence/{id}", permission: "site.read", what: "Evidence bundle with hashes and gaps" },
  { method: "GET", path: "/api/evidence-path/{node}", permission: "site.read", what: "Walk from a conclusion back to its sources (?aoi_id)" },
  { method: "POST", path: "/api/analysis", permission: "site.read", what: "Findings for a site and range (read-only; never triggers tasking)" },
  { method: "POST", path: "/api/copilot", permission: "copilot.ask", what: "Grounded question answering with citations" },
  { method: "GET", path: "/api/watchlists", permission: "site.read", what: "Watchlists", example: "/api/watchlists" },
  { method: "POST", path: "/api/watchlists", permission: "watchlist.manage", what: "Create a watchlist" },
  { method: "GET", path: "/api/rules", permission: "alert.read", what: "Alert rules", example: "/api/rules" },
  { method: "GET", path: "/api/reports", permission: "report.read", what: "Generated reports" },
  { method: "GET", path: "/api/audit", permission: "audit.read", what: "Hash-chained audit log (?limit)" },
  { method: "GET", path: "/api/sensing", permission: "any", what: "Capability matrix, or one target (?task&target)", example: "/api/sensing?task=detect" },
  { method: "GET", path: "/api/providers", permission: "imagery.read", what: "Imagery provider configuration (names only)", example: "/api/providers" },
];

const PY_VERIFY = `import hashlib, hmac, time

def verify(body: bytes, header: str, secret: str, window_s: int = 300) -> bool:
    """X-TerraShield-Signature: t=<unix>,v1=<hex hmac-sha256 of "t." + body>"""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts, given = int(parts["t"]), parts["v1"]
    except (ValueError, KeyError):
        return False
    if abs(int(time.time()) - ts) > window_s:      # replay protection
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given)`;

const NODE_VERIFY = `import crypto from "node:crypto";

export function verify(rawBody, header, secret, windowS = 300) {
  const parts = Object.fromEntries(header.split(",").map((p) => p.split("=", 2)));
  const ts = Number(parts.t);
  if (!ts || !parts.v1 || Math.abs(Date.now() / 1000 - ts) > windowS) return false;
  const expected = crypto.createHmac("sha256", secret)
    .update(Buffer.concat([Buffer.from(\`\${ts}.\`), rawBody])).digest("hex");
  return crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(parts.v1));
}`;

const curlFor = (path: string) => `curl -s -H "Authorization: Bearer $TERRASHIELD_TOKEN" \\
  "https://<your-terrashield-host>${path}"`;

const PY_CLIENT = `import os, requests

BASE = "https://<your-terrashield-host>"
s = requests.Session()
s.headers["Authorization"] = f"Bearer {os.environ['TERRASHIELD_TOKEN']}"

queue = s.get(f"{BASE}/api/alerts", params={"status": "pending", "max_priority": 2}).json()
for a in queue["alerts"]:
    ev = s.get(f"{BASE}/api/evidence/{a['evidence_id']}").json()
    print(a["priority"], a["title"], a["aoi_id"], ev["sha256"][:12])`;

function CodeBlock({ code }: { code: string }) {
  return (
    <div className="relative">
      <button className="btn btn-ghost absolute right-1 top-1 px-2 py-1 text-xs" onClick={() => navigator.clipboard?.writeText(code)}>
        <Copy className="h-3 w-3" /> Copy
      </button>
      <pre className="overflow-x-auto rounded-md border border-line bg-bg p-3 font-mono text-[12px] leading-relaxed">{code}</pre>
    </div>
  );
}

function TryIt() {
  const [path, setPath] = useState("/api/alerts?max_priority=2");
  const [out, setOut] = useState("");
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    try {
      const [p, q] = path.split("?");
      const query = Object.fromEntries(new URLSearchParams(q ?? ""));
      const r = await api<unknown>(p, { query });
      setOut(JSON.stringify(r, null, 2).slice(0, 20000));
    } catch (e) {
      setOut(`Error: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-2">
      <div className="flex gap-2">
        <span className="chip border-accent/50 px-2 text-accent">GET</span>
        <input className="input font-mono" value={path} onChange={(e) => setPath(e.target.value)} onKeyDown={(e) => e.key === "Enter" && run()} />
        <button className="btn btn-primary" onClick={run} disabled={busy || !path.startsWith("/api/")}>
          <Play className="h-4 w-4" /> Send
        </button>
      </div>
      <p className="text-[11px] text-dim">Runs with your session, so it returns exactly what your role may see. Read-only: GET only.</p>
      {out && <pre className="max-h-[420px] overflow-auto rounded-md border border-line bg-bg p-3 font-mono text-[11px]">{out}</pre>}
    </div>
  );
}

type T = "api" | "try" | "webhooks" | "sdk" | "deploy";

export default function Integrations() {
  const [tab, setTab] = useState<T>("api");
  const hasToken = !!getToken();
  return (
    <div className="space-y-4">
      <PageHeader title="Integrations" subtitle="The HTTP contract the console uses, webhooks for alert delivery, and the steps to connect a mission system." />
      <Tabs<T>
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "api", label: "API reference" },
          { id: "try", label: "Try it" },
          { id: "webhooks", label: "Webhooks" },
          { id: "sdk", label: "Client snippets" },
          { id: "deploy", label: "Connect & deploy" },
        ]}
      />
      {tab === "api" && (
        <Card bodyClass="p-0">
          <div className="overflow-x-auto">
            <table className="tbl">
              <thead>
                <tr>
                  <th>Method</th>
                  <th>Path</th>
                  <th>Permission</th>
                  <th>What it returns</th>
                </tr>
              </thead>
              <tbody>
                {ROUTES.map((r) => (
                  <tr key={r.method + r.path}>
                    <td><span className={`chip ${r.method === "GET" ? "border-info/50 text-info" : "border-high/50 text-high"}`}>{r.method}</span></td>
                    <td className="mono whitespace-nowrap">{r.path}</td>
                    <td><Tag>{r.permission}</Tag></td>
                    <td className="text-sm text-dim">{r.what}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="border-t border-line p-4 text-xs text-dim">
            Authenticate with <span className="mono">Authorization: Bearer &lt;token&gt;</span>. Dates are ISO (<span className="mono">2026-05-01</span>). Errors are{" "}
            <span className="mono">{`{"error", "hint"}`}</span> with a 4xx status; a 403 names the permission your role lacks.
          </div>
        </Card>
      )}
      {tab === "try" && <Card title="Request console">{hasToken ? <TryIt /> : <p className="text-sm text-dim">Sign in first.</p>}</Card>}
      {tab === "webhooks" && (
        <div className="grid gap-4 xl:grid-cols-2">
          <Card title="What is delivered">
            <div className="space-y-3 text-sm">
              <p>Alerts are POSTed to each configured destination with retries; failures are recorded and shown, never silent.</p>
              <ul className="list-disc space-y-1 pl-5 text-dim">
                <li>The payload carries <b>references only</b>: alert id, severity, site id and a URL — never the finding, its evidence or imagery.</li>
                <li><span className="mono">X-TerraShield-Signature: t=&lt;unix&gt;,v1=&lt;hex&gt;</span> — HMAC-SHA256 over <span className="mono">"t." + body</span>.</li>
                <li><span className="mono">X-TerraShield-Delivery: &lt;alert_id&gt;:&lt;attempt&gt;</span> — use it to make your receiver idempotent.</li>
                <li>Reject anything older than 300 s; the timestamp is inside the signed material, so a captured delivery cannot be replayed.</li>
                <li>Email needs an SMTP relay belonging to the deployment; without one the attempt is recorded as failed.</li>
              </ul>
            </div>
          </Card>
          <div className="space-y-4">
            <Card title="Verify — Python"><CodeBlock code={PY_VERIFY} /></Card>
            <Card title="Verify — Node.js"><CodeBlock code={NODE_VERIFY} /></Card>
          </div>
        </div>
      )}
      {tab === "sdk" && (
        <div className="grid gap-4 xl:grid-cols-2">
          <Card title="curl"><CodeBlock code={curlFor("/api/alerts?status=pending&max_priority=2")} /></Card>
          <Card title="Python"><CodeBlock code={PY_CLIENT} /></Card>
        </div>
      )}
      {tab === "deploy" && (
        <Card title="Connecting a deployment">
          <ol className="list-decimal space-y-3 pl-5 text-sm">
            <li>
              <b>Build the console</b> once, on a machine with npm: <span className="mono">cd frontend &amp;&amp; npm ci &amp;&amp; npm run build</span>. The output in{" "}
              <span className="mono">frontend/dist</span> is static and makes no outbound request, so it can be carried into an air-gapped site.
            </li>
            <li>
              <b>Serve it</b> from the API process: <span className="mono">terrashield --db terrashield.db serve</span>, then open <span className="mono">/console/</span>. One origin,
              so there is no CORS to configure.
            </li>
            <li>
              <b>Create accounts</b> with <span className="mono">terrashield register</span> on the host; service integrations use a token set with{" "}
              <span className="mono">TERRASHIELD_TOKEN</span>.
            </li>
            <li>
              <b>Select imagery</b> with <span className="mono">TERRASHIELD_PROVIDER</span> and the variables the Data sources page lists as missing.
            </li>
            <li>
              <b>Wire alerts</b> to a webhook receiver that verifies the signature (Webhooks tab) and forwards references into your C4I or ticketing system.
            </li>
            <li>
              <b>Put TLS in front</b> (nginx, Caddy, or the facility's gateway). The console's CSP allows only same-origin scripts, styles and connections.
            </li>
          </ol>
          <div className="mt-4">
            <Caveat>Full steps, including development with hot reload against a local API, are in docs/05-console.md.</Caveat>
          </div>
        </Card>
      )}
    </div>
  );
}
