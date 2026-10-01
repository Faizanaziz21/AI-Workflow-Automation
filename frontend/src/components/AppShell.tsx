"use client";

import {
  Activity,
  Bot,
  CheckSquare,
  FileSearch,
  LayoutDashboard,
  LayoutTemplate,
  LogOut,
  Moon,
  Plug,
  Settings,
  Sun,
  Workflow,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { logout } from "@/lib/api";
import { useAuth, useData } from "@/lib/auth";
import { cn, Select } from "./ui";

const NAV = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard, perm: "dashboard:read" },
  { href: "/workflows", label: "Workflows", icon: Workflow, perm: "workflows:read" },
  { href: "/executions", label: "Executions", icon: Activity, perm: "executions:read" },
  { href: "/approvals", label: "Approvals", icon: CheckSquare, perm: "approvals:read" },
  { href: "/templates", label: "Templates", icon: LayoutTemplate, perm: "workflows:read" },
  { href: "/connections", label: "Connections", icon: Plug, perm: "connections:read" },
  { href: "/ai", label: "AI usage", icon: Bot, perm: "dashboard:read" },
  { href: "/audit", label: "Audit log", icon: FileSearch, perm: "audit:read" },
  { href: "/settings", label: "Settings", icon: Settings, perm: "users:read" },
];

function ThemeToggle() {
  const [theme, setTheme] = useState<"light" | "dark">("light");
  useEffect(() => {
    let saved: string | null = null;
    try {
      saved = localStorage.getItem("ff.theme");
    } catch {
      /* ignore */
    }
    const initial = (saved as "light" | "dark") ?? (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    setTheme(initial);
    document.documentElement.dataset.theme = initial;
  }, []);
  const toggle = () => {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("ff.theme", next);
    } catch {
      /* ignore */
    }
  };
  return (
    <button onClick={toggle} className="rounded-md p-2 text-ink-2 hover:bg-surface-2" aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}>
      {theme === "dark" ? <Sun className="size-4" /> : <Moon className="size-4" />}
    </button>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { me, workspaces, workspace, setWorkspaceId, can } = useAuth();
  const approvals = useData<{ total: number }>(me ? "/approvals" : null, { limit: 1 }, 30000);

  if (pathname === "/login") return <>{children}</>;
  const fullBleed = /^\/workflows\/[^/]+$/.test(pathname);

  return (
    <div className="flex h-screen overflow-hidden">
      <nav className="flex w-56 shrink-0 flex-col border-r border-line bg-surface" aria-label="Main">
        <div className="flex h-14 items-center gap-2 border-b border-line px-4">
          <div className="flex size-7 items-center justify-center rounded-md bg-accent text-sm font-bold text-white">F</div>
          <div className="leading-tight">
            <div className="text-sm font-semibold">FlowForge AI</div>
            <div className="text-[11px] text-muted">{me?.organization.name}</div>
          </div>
        </div>
        <ul className="flex-1 space-y-0.5 overflow-y-auto p-2">
          {NAV.filter((n) => can(n.perm)).map((n) => {
            const active = n.href === "/" ? pathname === "/" : pathname.startsWith(n.href);
            return (
              <li key={n.href}>
                <Link
                  href={n.href}
                  className={cn(
                    "flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm",
                    active ? "bg-accent-soft font-medium text-accent" : "text-ink-2 hover:bg-surface-2 hover:text-ink",
                  )}
                >
                  <n.icon className="size-4" aria-hidden />
                  <span className="flex-1">{n.label}</span>
                  {n.href === "/approvals" && (approvals.data?.total ?? 0) > 0 && (
                    <span className="rounded-full bg-warning/25 px-1.5 text-xs font-semibold text-ink tabular">{approvals.data?.total}</span>
                  )}
                </Link>
              </li>
            );
          })}
        </ul>
        <div className="border-t border-line p-3 text-xs text-ink-2">
          <div className="truncate font-medium text-ink">{me?.user?.full_name ?? "API key"}</div>
          <div className="truncate">{me?.user?.email}</div>
          <div className="mt-1 truncate text-muted">{(me?.roles ?? []).join(", ") || "workspace roles"}</div>
        </div>
      </nav>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center justify-between gap-3 border-b border-line bg-surface px-4">
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted">Workspace</span>
            <Select
              aria-label="Workspace"
              className="h-8 w-56"
              value={workspace?.id ?? ""}
              onChange={(e) => setWorkspaceId(e.target.value)}
            >
              {workspaces.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name}
                </option>
              ))}
            </Select>
          </div>
          <div className="flex items-center gap-1">
            <ThemeToggle />
            <button
              className="flex items-center gap-1.5 rounded-md px-2.5 py-2 text-sm text-ink-2 hover:bg-surface-2"
              onClick={async () => {
                await logout();
                router.replace("/login");
              }}
            >
              <LogOut className="size-4" /> Sign out
            </button>
          </div>
        </header>
        <main className={cn("min-h-0 flex-1", fullBleed ? "overflow-hidden" : "overflow-y-auto p-6")}>{children}</main>
      </div>
    </div>
  );
}
