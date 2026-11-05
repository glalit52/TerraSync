import { useState } from "react";
import { KeyRound, Loader2, LogIn, ShieldCheck, UserPlus } from "lucide-react";
import { ApiError } from "../api/client";
import { useAuth } from "../state/auth";

type Mode = "login" | "signup" | "token";

export function Login() {
  const { login, signup, useToken } = useAuth();
  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [org, setOrg] = useState("");
  const [name, setName] = useState("");
  const [token, setTokenInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<{ msg: string; hint?: string } | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      if (mode === "login") await login(email, password);
      else if (mode === "signup") await signup(org, email, password, name);
      else await useToken(token);
    } catch (x) {
      const a = x as ApiError;
      setErr({ msg: a.message, hint: a.hint });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid min-h-full lg:grid-cols-[1.1fr_1fr]">
      <div className="relative hidden overflow-hidden border-r border-line bg-panel lg:block">
        <div className="map-grid absolute inset-0 opacity-60" />
        <svg className="absolute inset-0 h-full w-full" viewBox="0 0 600 600" preserveAspectRatio="xMidYMid slice" aria-hidden>
          <defs>
            <radialGradient id="glow" cx="50%" cy="50%" r="50%">
              <stop offset="0%" stopColor="rgb(163 230 53)" stopOpacity="0.25" />
              <stop offset="100%" stopColor="rgb(163 230 53)" stopOpacity="0" />
            </radialGradient>
          </defs>
          <circle cx="300" cy="320" r="220" fill="url(#glow)" />
          {[90, 150, 210].map((r) => (
            <ellipse key={r} cx="300" cy="320" rx={r * 1.35} ry={r * 0.5} fill="none" stroke="rgb(56 189 248)" strokeOpacity="0.25" strokeDasharray="3 5" transform={`rotate(-18 300 320)`} />
          ))}
          <circle cx="300" cy="320" r="70" fill="none" stroke="rgb(163 230 53)" strokeOpacity="0.6" />
          <polygon points="420,180 300,320 330,330" fill="rgb(163 230 53)" fillOpacity="0.12" stroke="rgb(163 230 53)" strokeOpacity="0.5" />
          <rect x="412" y="170" width="18" height="10" fill="rgb(226 232 240)" />
        </svg>
        <div className="relative flex h-full flex-col justify-between p-10">
          <div className="text-sm font-semibold tracking-[0.3em] text-dim">SENSEGRASS</div>
          <div>
            <h1 className="text-5xl font-bold tracking-tight">
              Terra<span className="text-accent">Shield</span>
            </h1>
            <p className="mt-3 max-w-md text-lg text-dim">Change intelligence from orbit. Every finding carries the evidence it rests on.</p>
            <ul className="mt-8 space-y-2 text-sm text-dim">
              {[
                "Detect, compare and baseline every monitored site",
                "Ranked alert queue with human review on every finding",
                "Evidence path from conclusion back to the pixels",
                "Runs offline: no tile server, no CDN, no outbound calls",
              ].map((t) => (
                <li key={t} className="flex items-center gap-2">
                  <ShieldCheck className="h-4 w-4 text-accent" /> {t}
                </li>
              ))}
            </ul>
          </div>
          <p className="text-xs text-dim">Analyst-assistance and monitoring system. Not a targeting, attribution or autonomous decision system.</p>
        </div>
      </div>

      <div className="flex items-center justify-center p-6">
        <form onSubmit={submit} className="w-full max-w-sm">
          <h2 className="text-xl font-semibold">{mode === "signup" ? "Create an organisation" : "Sign in"}</h2>
          <p className="mt-1 text-sm text-dim">
            {mode === "token" ? "Use a service token issued with TERRASHIELD_TOKEN." : "Use the account your administrator created."}
          </p>
          <div className="mt-5 grid grid-cols-3 gap-1 rounded-md border border-line p-1 text-xs">
            {(
              [
                ["login", "Account", <LogIn key="l" className="h-3.5 w-3.5" />],
                ["signup", "Sign up", <UserPlus key="s" className="h-3.5 w-3.5" />],
                ["token", "Token", <KeyRound key="t" className="h-3.5 w-3.5" />],
              ] as const
            ).map(([m, label, icon]) => (
              <button
                key={m}
                type="button"
                onClick={() => (setMode(m), setErr(null))}
                className={`flex items-center justify-center gap-1 rounded px-2 py-1.5 ${mode === m ? "bg-raised text-ink" : "text-dim"}`}
              >
                {icon} {label}
              </button>
            ))}
          </div>

          <div className="mt-5 space-y-3">
            {mode === "signup" && (
              <>
                <label className="block">
                  <span className="label">Organisation</span>
                  <input className="input" required value={org} onChange={(e) => setOrg(e.target.value)} />
                </label>
                <label className="block">
                  <span className="label">Your name</span>
                  <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
                </label>
              </>
            )}
            {mode !== "token" ? (
              <>
                <label className="block">
                  <span className="label">Email</span>
                  <input className="input" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
                </label>
                <label className="block">
                  <span className="label">Password</span>
                  <input
                    className="input"
                    type="password"
                    autoComplete={mode === "signup" ? "new-password" : "current-password"}
                    required
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                  />
                </label>
              </>
            ) : (
              <label className="block">
                <span className="label">Bearer token</span>
                <input className="input font-mono" required value={token} onChange={(e) => setTokenInput(e.target.value)} />
              </label>
            )}
          </div>

          {err && (
            <div className="mt-4 rounded-md border border-crit/40 bg-crit/5 p-3 text-sm text-crit" role="alert">
              {err.msg}
              {err.hint && <div className="mt-1 text-xs text-dim">{err.hint}</div>}
            </div>
          )}

          <button className="btn btn-primary mt-5 w-full justify-center py-2" disabled={busy}>
            {busy && <Loader2 className="h-4 w-4 animate-spin" />}
            {mode === "signup" ? "Create organisation" : "Continue"}
          </button>
          {mode === "signup" && (
            <p className="mt-3 text-xs text-dim">Sign-up only works when the server was started with --allow-signup. Otherwise an administrator creates accounts.</p>
          )}
        </form>
      </div>
    </div>
  );
}
