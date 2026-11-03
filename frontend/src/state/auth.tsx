import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, getToken, onTokenChange, setToken } from "../api/client";
import type { Me, Session } from "../api/types";

interface AuthCtx {
  token: string | null;
  me: Me | null;
  loading: boolean;
  can: (permission: string) => boolean;
  login: (email: string, password: string) => Promise<void>;
  signup: (organisation: string, email: string, password: string, name: string) => Promise<void>;
  useToken: (token: string) => Promise<void>;
  logout: () => Promise<void>;
}

const Ctx = createContext<AuthCtx | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const [token, setTok] = useState<string | null>(getToken());
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState<boolean>(!!getToken());

  useEffect(() => {
    const off = onTokenChange(() => setTok(getToken()));
    return () => {
      off();
    };
  }, []);

  useEffect(() => {
    if (!token) {
      setMe(null);
      setLoading(false);
      qc.clear();
      return;
    }
    let live = true;
    setLoading(true);
    api<Me>("/api/me")
      .then((m) => live && setMe(m))
      .catch(() => live && setToken(null))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [token, qc]);

  const login = useCallback(async (email: string, password: string) => {
    const s = await api<Session>("/api/auth/login", { method: "POST", body: { email, password } });
    setToken(s.token);
  }, []);

  const signup = useCallback(async (organisation: string, email: string, password: string, name: string) => {
    const s = await api<Session>("/api/auth/signup", {
      method: "POST",
      body: { organisation, email, password, name },
    });
    setToken(s.token);
  }, []);

  // A service token (TERRASHIELD_TOKEN) has no sign-in step; it is pasted.
  const useServiceToken = useCallback(async (t: string) => {
    setToken(t.trim());
  }, []);

  const logout = useCallback(async () => {
    try {
      await api("/api/auth/logout", { method: "POST" });
    } catch {
      /* the session is dropped locally either way */
    }
    setToken(null);
  }, []);

  const value = useMemo<AuthCtx>(
    () => ({
      token,
      me,
      loading,
      can: (p) => !!me?.permissions.includes(p),
      login,
      signup,
      useToken: useServiceToken,
      logout,
    }),
    [token, me, loading, login, signup, useServiceToken, logout],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}
