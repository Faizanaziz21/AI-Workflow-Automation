"use client";

import { usePathname, useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api, refreshSession } from "./api";
import type { Me, Workspace } from "./types";

type AuthState = {
  me: Me | null;
  workspaces: Workspace[];
  workspace: Workspace | null;
  setWorkspaceId: (id: string) => void;
  can: (permission: string) => boolean;
  reload: () => Promise<void>;
};

const Ctx = createContext<AuthState | null>(null);
const WS_KEY = "ff.workspace";

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspaceId, setWorkspaceIdState] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const router = useRouter();
  const pathname = usePathname();

  const reload = useCallback(async () => {
    const [m, ws] = await Promise.all([api<Me>("/auth/me"), api<Workspace[]>("/workspaces")]);
    setMe(m);
    setWorkspaces(ws);
    let saved: string | null = null;
    try {
      saved = localStorage.getItem(WS_KEY);
    } catch {
      /* storage unavailable */
    }
    setWorkspaceIdState((cur) => cur ?? (ws.find((w) => w.id === saved)?.id ?? ws[0]?.id ?? null));
  }, []);

  useEffect(() => {
    if (pathname === "/login") {
      setReady(true);
      return;
    }
    let cancelled = false;
    (async () => {
      const ok = me ? true : await refreshSession();
      if (!ok) {
        if (!cancelled) router.replace(`/login?next=${encodeURIComponent(pathname)}`);
        return;
      }
      if (!me) await reload().catch(() => router.replace("/login"));
      if (!cancelled) setReady(true);
    })();
    return () => {
      cancelled = true;
    };
  }, [pathname, me, reload, router]);

  const setWorkspaceId = useCallback((id: string) => {
    setWorkspaceIdState(id);
    try {
      localStorage.setItem(WS_KEY, id);
    } catch {
      /* ignore */
    }
  }, []);

  const value = useMemo<AuthState>(() => {
    const workspace = workspaces.find((w) => w.id === workspaceId) ?? null;
    return {
      me,
      workspaces,
      workspace,
      setWorkspaceId,
      reload,
      can: (permission: string) => !!me && me.permissions.includes(permission),
    };
  }, [me, workspaces, workspaceId, setWorkspaceId, reload]);

  if (!ready && pathname !== "/login") {
    return (
      <div className="flex h-screen items-center justify-center text-sm text-muted" role="status">
        Loading FlowForge…
      </div>
    );
  }
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside AuthProvider");
  return v;
}

export function useData<T>(path: string | null, query?: Record<string, unknown>, intervalMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const key = path ? path + JSON.stringify(query ?? {}) : null;

  const load = useCallback(async () => {
    if (!path) return;
    try {
      const d = await api<T>(path, { query });
      setData(d);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  useEffect(() => {
    setLoading(true);
    load();
    if (!intervalMs) return;
    const t = setInterval(load, intervalMs);
    return () => clearInterval(t);
  }, [load, intervalMs]);

  return { data, error, loading, reload: load, setData };
}
