/** Typed fetch wrapper: bearer auth from memory, transparent refresh via HttpOnly cookie + CSRF header. */

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details?: unknown,
  ) {
    super(message);
  }
}

let accessToken: string | null = null;
let refreshing: Promise<boolean> | null = null;
const listeners = new Set<(authed: boolean) => void>();

export function setAccessToken(token: string | null) {
  accessToken = token;
  listeners.forEach((l) => l(!!token));
}

export function onAuthChange(fn: (authed: boolean) => void) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function csrfToken(): string {
  if (typeof document === "undefined") return "";
  const m = document.cookie.match(/(?:^|; )ff_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

export async function refreshSession(): Promise<boolean> {
  if (!refreshing) {
    refreshing = (async () => {
      try {
        const r = await fetch("/api/v1/auth/refresh", {
          method: "POST",
          credentials: "same-origin",
          headers: { "X-CSRF-Token": csrfToken() },
        });
        if (!r.ok) {
          setAccessToken(null);
          return false;
        }
        const body = await r.json();
        setAccessToken(body.access_token);
        return true;
      } catch {
        return false;
      } finally {
        setTimeout(() => (refreshing = null), 0);
      }
    })();
  }
  return refreshing;
}

type Options = Omit<RequestInit, "body"> & { body?: unknown; query?: Record<string, unknown>; raw?: boolean };

export async function api<T = unknown>(path: string, opts: Options = {}, retried = false): Promise<T> {
  const url = new URL(`/api/v1${path}`, window.location.origin);
  for (const [k, v] of Object.entries(opts.query ?? {})) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) v.forEach((x) => url.searchParams.append(k, String(x)));
    else url.searchParams.set(k, String(v));
  }
  const headers: Record<string, string> = { ...(opts.headers as Record<string, string>) };
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
  let body: BodyInit | undefined;
  if (opts.body instanceof FormData) body = opts.body;
  else if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  const res = await fetch(url.toString(), { ...opts, headers, body, credentials: "same-origin" });
  if (res.status === 401 && !retried && path !== "/auth/login") {
    if (await refreshSession()) return api<T>(path, opts, true);
  }
  if (!res.ok) {
    let payload: { error?: { code?: string; message?: string; details?: unknown } } = {};
    try {
      payload = await res.json();
    } catch {
      /* non-JSON error */
    }
    throw new ApiError(res.status, payload.error?.code ?? "error", payload.error?.message ?? res.statusText, payload.error?.details);
  }
  if (res.status === 204) return undefined as T;
  if (opts.raw) return res as unknown as T;
  return (await res.json()) as T;
}

export async function login(email: string, password: string) {
  const r = await api<{ access_token: string }>("/auth/login", { method: "POST", body: { email, password } });
  setAccessToken(r.access_token);
}

export async function logout() {
  try {
    await api("/auth/logout", { method: "POST", headers: { "X-CSRF-Token": csrfToken() } });
  } finally {
    setAccessToken(null);
  }
}
