"use client";

import clsx from "clsx";
import {
  AlertOctagon,
  AlertTriangle,
  Ban,
  CheckCircle2,
  CircleDashed,
  Clock,
  Hourglass,
  Loader2,
  PauseCircle,
  RotateCw,
  SkipForward,
  UserCheck,
  X,
} from "lucide-react";
import { createContext, useCallback, useContext, useEffect, useState } from "react";

export function cn(...args: Parameters<typeof clsx>) {
  return clsx(...args);
}

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md";
  loading?: boolean;
  icon?: React.ReactNode;
};

export function Button({ variant = "secondary", size = "md", loading, icon, className, children, disabled, ...rest }: ButtonProps) {
  return (
    <button
      {...rest}
      disabled={disabled || loading}
      className={cn(
        "inline-flex items-center justify-center gap-1.5 rounded-md font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50",
        size === "sm" ? "h-8 px-2.5 text-xs" : "h-9 px-3.5 text-sm",
        variant === "primary" && "bg-accent text-white hover:opacity-90",
        variant === "secondary" && "border border-line bg-surface text-ink hover:bg-surface-2",
        variant === "ghost" && "text-ink-2 hover:bg-surface-2 hover:text-ink",
        variant === "danger" && "bg-critical text-white hover:opacity-90",
        className,
      )}
    >
      {loading ? <Loader2 className="size-4 animate-spin" aria-hidden /> : icon}
      {children}
    </button>
  );
}

export function Card({ className, children, title, actions }: { className?: string; children: React.ReactNode; title?: React.ReactNode; actions?: React.ReactNode }) {
  return (
    <section className={cn("rounded-lg border border-line bg-surface", className)}>
      {(title || actions) && (
        <header className="flex items-center justify-between gap-2 border-b border-line px-4 py-3">
          <h2 className="text-sm font-semibold text-ink">{title}</h2>
          <div className="flex items-center gap-2">{actions}</div>
        </header>
      )}
      {children}
    </section>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: React.ReactNode; actions?: React.ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-ink">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-ink-2">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export const inputClass =
  "h-9 w-full rounded-md border border-line bg-surface px-2.5 text-sm text-ink placeholder:text-muted focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/25";

export function Input(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={cn(inputClass, props.className)} />;
}

export function Textarea(props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...props} className={cn(inputClass, "h-auto min-h-20 py-2 font-mono text-xs", props.className)} />;
}

export function Select(props: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...props} className={cn(inputClass, "pr-8", props.className)} />;
}

export function Field({ label, hint, children, required }: { label: string; hint?: React.ReactNode; children: React.ReactNode; required?: boolean }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-ink-2">
        {label}
        {required && <span className="text-critical"> *</span>}
      </span>
      {children}
      {hint && <span className="mt-1 block text-xs text-muted">{hint}</span>}
    </label>
  );
}

const STATUS: Record<string, { label: string; tone: string; Icon: typeof CheckCircle2 }> = {
  COMPLETED: { label: "Completed", tone: "good", Icon: CheckCircle2 },
  RUNNING: { label: "Running", tone: "info", Icon: Loader2 },
  PENDING: { label: "Pending", tone: "neutral", Icon: CircleDashed },
  SCHEDULED: { label: "Scheduled", tone: "neutral", Icon: CircleDashed },
  WAITING: { label: "Waiting", tone: "warning", Icon: Hourglass },
  WAITING_APPROVAL: { label: "Awaiting approval", tone: "warning", Icon: UserCheck },
  RETRYING: { label: "Retrying", tone: "serious", Icon: RotateCw },
  PAUSED: { label: "Paused", tone: "neutral", Icon: PauseCircle },
  FAILED: { label: "Failed", tone: "critical", Icon: AlertOctagon },
  CANCELLED: { label: "Cancelled", tone: "neutral", Icon: Ban },
  SKIPPED: { label: "Skipped", tone: "muted", Icon: SkipForward },
  pending: { label: "Pending", tone: "warning", Icon: Clock },
  approved: { label: "Approved", tone: "good", Icon: CheckCircle2 },
  rejected: { label: "Rejected", tone: "critical", Icon: AlertOctagon },
  expired: { label: "Expired", tone: "serious", Icon: AlertTriangle },
  cancelled: { label: "Cancelled", tone: "neutral", Icon: Ban },
  connected: { label: "Connected", tone: "good", Icon: CheckCircle2 },
  error: { label: "Error", tone: "critical", Icon: AlertOctagon },
  untested: { label: "Untested", tone: "neutral", Icon: CircleDashed },
  pending_authorization: { label: "Needs authorization", tone: "warning", Icon: AlertTriangle },
  published: { label: "Published", tone: "good", Icon: CheckCircle2 },
  draft: { label: "Draft", tone: "neutral", Icon: CircleDashed },
  archived: { label: "Archived", tone: "muted", Icon: Ban },
  success: { label: "Success", tone: "good", Icon: CheckCircle2 },
  failure: { label: "Failure", tone: "critical", Icon: AlertOctagon },
  denied: { label: "Denied", tone: "critical", Icon: Ban },
};

const TONE: Record<string, string> = {
  good: "text-good-ink bg-good/10 ring-good/30",
  info: "text-accent bg-accent-soft ring-accent/30",
  warning: "text-ink bg-warning/15 ring-warning/40",
  serious: "text-ink bg-serious/15 ring-serious/40",
  critical: "text-critical bg-critical/10 ring-critical/30",
  neutral: "text-ink-2 bg-surface-2 ring-line",
  muted: "text-muted bg-surface-2 ring-line",
};

/** Status always pairs icon + label: color never carries meaning alone. */
export function StatusBadge({ status, className }: { status: string; className?: string }) {
  const s = STATUS[status] ?? { label: status, tone: "neutral", Icon: CircleDashed };
  const iconColor: Record<string, string> = {
    good: "text-good", info: "text-accent", warning: "text-warning", serious: "text-serious",
    critical: "text-critical", neutral: "text-muted", muted: "text-muted",
  };
  return (
    <span className={cn("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset", TONE[s.tone], className)}>
      <s.Icon className={cn("size-3.5", iconColor[s.tone], status === "RUNNING" && "animate-spin")} aria-hidden />
      {s.label}
    </span>
  );
}

export function Badge({ children, className }: { children: React.ReactNode; className?: string }) {
  return <span className={cn("inline-flex items-center rounded-md bg-surface-2 px-1.5 py-0.5 text-xs text-ink-2 ring-1 ring-inset ring-line", className)}>{children}</span>;
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 p-6 text-sm text-muted" role="status">
      <Loader2 className="size-4 animate-spin" aria-hidden /> {label}…
    </div>
  );
}

export function EmptyState({ title, body, action }: { title: string; body?: string; action?: React.ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-14 text-center">
      <p className="text-sm font-medium text-ink">{title}</p>
      {body && <p className="max-w-md text-sm text-ink-2">{body}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

export function ErrorNote({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div role="alert" className="flex items-start gap-2 rounded-md bg-critical/10 px-3 py-2 text-sm text-critical ring-1 ring-inset ring-critical/30">
      <AlertOctagon className="mt-0.5 size-4 shrink-0" aria-hidden />
      <span>{message}</span>
    </div>
  );
}

export function Modal({ open, onClose, title, children, footer, wide }: { open: boolean; onClose: () => void; title: string; children: React.ReactNode; footer?: React.ReactNode; wide?: boolean }) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/40 p-4 pt-16" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onMouseDown={(e) => e.stopPropagation()}
        className={cn("w-full rounded-lg border border-line bg-surface shadow-xl", wide ? "max-w-3xl" : "max-w-lg")}
      >
        <header className="flex items-center justify-between border-b border-line px-4 py-3">
          <h2 className="text-sm font-semibold">{title}</h2>
          <button onClick={onClose} aria-label="Close" className="rounded p-1 text-muted hover:bg-surface-2 hover:text-ink">
            <X className="size-4" />
          </button>
        </header>
        <div className="max-h-[70vh] overflow-y-auto p-4">{children}</div>
        {footer && <footer className="flex justify-end gap-2 border-t border-line px-4 py-3">{footer}</footer>}
      </div>
    </div>
  );
}

export function Drawer({ open, onClose, title, children, actions }: { open: boolean; onClose: () => void; title: React.ReactNode; children: React.ReactNode; actions?: React.ReactNode }) {
  if (!open) return null;
  return (
    <aside className="fixed inset-y-0 right-0 z-40 flex w-full max-w-xl flex-col border-l border-line bg-surface shadow-2xl" aria-label="Details">
      <header className="flex items-center justify-between gap-2 border-b border-line px-4 py-3">
        <div className="min-w-0 text-sm font-semibold">{title}</div>
        <div className="flex items-center gap-2">
          {actions}
          <button onClick={onClose} aria-label="Close" className="rounded p-1 text-muted hover:bg-surface-2 hover:text-ink">
            <X className="size-4" />
          </button>
        </div>
      </header>
      <div className="flex-1 overflow-y-auto p-4">{children}</div>
    </aside>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: { id: T; label: React.ReactNode }[]; value: T; onChange: (v: T) => void }) {
  return (
    <div role="tablist" className="flex gap-1 border-b border-line">
      {tabs.map((t) => (
        <button
          key={t.id}
          role="tab"
          aria-selected={value === t.id}
          onClick={() => onChange(t.id)}
          className={cn(
            "-mb-px border-b-2 px-3 py-2 text-sm",
            value === t.id ? "border-accent font-medium text-ink" : "border-transparent text-ink-2 hover:text-ink",
          )}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function JsonView({ value, className }: { value: unknown; className?: string }) {
  const text = value === undefined ? "—" : JSON.stringify(value, null, 2);
  return (
    <pre className={cn("max-h-96 overflow-auto rounded-md bg-surface-2 p-3 font-mono text-xs leading-relaxed text-ink ring-1 ring-inset ring-line", className)}>
      {text}
    </pre>
  );
}

type Toast = { id: number; kind: "success" | "error"; text: string };
const ToastCtx = createContext<(kind: Toast["kind"], text: string) => void>(() => undefined);

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((kind: Toast["kind"], text: string) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, kind, text }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4500);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-80 flex-col gap-2" aria-live="polite">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={cn(
              "pointer-events-auto flex items-start gap-2 rounded-md border border-line bg-surface px-3 py-2 text-sm shadow-lg",
              t.kind === "error" ? "text-critical" : "text-ink",
            )}
          >
            {t.kind === "error" ? <AlertOctagon className="mt-0.5 size-4 shrink-0" /> : <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-good" />}
            <span>{t.text}</span>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export function useToast() {
  return useContext(ToastCtx);
}

export function Table({ head, children, empty }: { head: React.ReactNode[]; children: React.ReactNode; empty?: boolean }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-line text-xs uppercase tracking-wide text-muted">
          <tr>
            {head.map((h, i) => (
              <th key={i} className="whitespace-nowrap px-4 py-2 font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-[var(--border)]">{children}</tbody>
      </table>
      {empty && <EmptyState title="Nothing here yet" />}
    </div>
  );
}
