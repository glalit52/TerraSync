import type { ApiErrorBody } from "./types";

// One fetch wrapper for every call. Same-origin only: the console is served by
// the API process, so a relative path is the whole of the configuration and
// there is no base URL to get wrong in a facility.

const TOKEN_KEY = "terrashield.token";

let token: string | null = null;
try {
  // sessionStorage, not localStorage: a session should end with the tab on a
  // shared operations workstation. The server still enforces expiry.
  token = sessionStorage.getItem(TOKEN_KEY);
} catch {
  token = null;
}

const listeners = new Set<() => void>();

export function getToken(): string | null {
  return token;
}

export function setToken(next: string | null): void {
  token = next;
  try {
    if (next) sessionStorage.setItem(TOKEN_KEY, next);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: the token lives for this page only */
  }
  listeners.forEach((fn) => fn());
}

export function onTokenChange(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export class ApiError extends Error {
  status: number;
  hint?: string;
  constructor(status: number, body: ApiErrorBody) {
    super(body.error || `request failed (${status})`);
    this.status = status;
    this.hint = body.hint;
  }
}

type Query = Record<string, string | number | undefined | null>;

export function qs(params: Query = {}): string {
  const parts = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  return parts.length ? `?${parts.join("&")}` : "";
}

export async function api<T>(
  path: string,
  opts: { method?: "GET" | "POST"; query?: Query; body?: unknown; signal?: AbortSignal } = {},
): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";

  let res: Response;
  try {
    res = await fetch(path + qs(opts.query), {
      method: opts.method ?? "GET",
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      signal: opts.signal,
      credentials: "same-origin",
    });
  } catch (e) {
    if ((e as Error).name === "AbortError") throw e;
    throw new ApiError(0, {
      error: "the TerraShield server could not be reached",
      hint: "check that `terrashield serve` is running",
    });
  }

  let body: unknown = null;
  const text = await res.text();
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = { error: text.slice(0, 200) };
    }
  }
  if (!res.ok) {
    // A 401 on an authenticated call means the session is gone: drop it so
    // the app returns to sign-in instead of showing a page of errors.
    if (res.status === 401 && token && !path.startsWith("/api/auth/")) setToken(null);
    throw new ApiError(res.status, (body as ApiErrorBody) ?? { error: res.statusText });
  }
  return body as T;
}
