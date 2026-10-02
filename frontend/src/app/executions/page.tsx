"use client";

import Link from "next/link";
import { useState } from "react";
import { Badge, Button, Card, EmptyState, Input, PageHeader, Select, Spinner, StatusBadge, Table, Tabs, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { useAuth, useData } from "@/lib/auth";
import { ago, duration } from "@/lib/format";
import type { ExecutionSummary } from "@/lib/types";

const STATUSES = ["", "RUNNING", "WAITING", "WAITING_APPROVAL", "RETRYING", "PAUSED", "COMPLETED", "FAILED", "CANCELLED"];

type DeadLetter = {
  id: string;
  execution_id: string | null;
  workflow_name: string | null;
  node_id: string | null;
  source: string;
  kind: string;
  error: string;
  attempts: number;
  created_at: string;
  resolved_at: string | null;
  resolution: string | null;
};

export default function ExecutionsPage() {
  const { can } = useAuth();
  const [tab, setTab] = useState<"executions" | "dead">("executions");
  return (
    <div className="mx-auto max-w-7xl">
      <PageHeader title="Executions" subtitle="Every run, its status and timeline. Failed work that needs an operator lands in dead letters." />
      {can("dead_letters:manage") && (
        <div className="mb-4">
          <Tabs value={tab} onChange={setTab} tabs={[{ id: "executions", label: "All executions" }, { id: "dead", label: "Dead letters" }]} />
        </div>
      )}
      {tab === "executions" ? <ExecutionList /> : <DeadLetters />}
    </div>
  );
}

function DeadLetters() {
  const toast = useToast();
  const [showResolved, setShowResolved] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const list = useData<DeadLetter[]>("/dead-letters", { include_resolved: showResolved || undefined, limit: 200 }, 10000);
  const act = async (d: DeadLetter, action: "requeue" | "resolve") => {
    setBusy(d.id);
    try {
      await api(`/dead-letters/${d.id}/${action}`, { method: "POST" });
      toast("success", action === "requeue" ? "Requeued: failed nodes will run again" : "Dismissed");
      list.reload();
    } catch (e) {
      toast("error", (e as Error).message);
    } finally {
      setBusy(null);
    }
  };
  return (
    <Card
      title="Dead letters"
      actions={
        <label className="flex items-center gap-2 text-xs text-ink-2">
          <input type="checkbox" checked={showResolved} onChange={(e) => setShowResolved(e.target.checked)} /> Show resolved
        </label>
      }
    >
      {!list.data ? (
        <Spinner />
      ) : list.data.length === 0 ? (
        <EmptyState title="No dead letters" body="Executions that fail after exhausting their retries appear here for an operator to requeue or dismiss." />
      ) : (
        <Table head={["Failed", "Workflow", "Node", "Error", "Attempts", "Status", ""]}>
          {list.data.map((d) => (
            <tr key={d.id} className="hover:bg-surface-2">
              <td className="whitespace-nowrap px-4 py-2 text-ink-2">{ago(d.created_at)}</td>
              <td className="px-4 py-2">
                {d.execution_id ? (
                  <Link href={`/executions/${d.execution_id}`} className="font-medium hover:text-accent">{d.workflow_name ?? d.execution_id.slice(0, 8)}</Link>
                ) : (
                  <span className="font-mono text-xs">{d.kind}</span>
                )}
              </td>
              <td className="px-4 py-2 font-mono text-xs text-ink-2">{d.node_id ?? "—"}</td>
              <td className="max-w-md truncate px-4 py-2 text-critical" title={d.error}>{d.error}</td>
              <td className="px-4 py-2 tabular">{d.attempts}</td>
              <td className="px-4 py-2">{d.resolved_at ? <Badge>{d.resolution}</Badge> : <StatusBadge status="FAILED" />}</td>
              <td className="whitespace-nowrap px-4 py-2 text-right">
                {!d.resolved_at && (
                  <div className="flex justify-end gap-1">
                    <Button size="sm" loading={busy === d.id} onClick={() => act(d, "requeue")}>Requeue</Button>
                    <Button size="sm" variant="ghost" disabled={busy === d.id} onClick={() => act(d, "resolve")}>Dismiss</Button>
                  </div>
                )}
              </td>
            </tr>
          ))}
        </Table>
      )}
    </Card>
  );
}

function ExecutionList() {
  const { workspace } = useAuth();
  const [status, setStatus] = useState("");
  const [trigger, setTrigger] = useState("");
  const [correlation, setCorrelation] = useState("");
  const list = useData<{ items: ExecutionSummary[]; total: number }>(
    "/executions",
    { workspace_id: workspace?.id, status: status || undefined, trigger_type: trigger || undefined, correlation_key: correlation || undefined, limit: 100 },
    5000,
  );

  return (
    <Card>
      <div className="flex flex-wrap gap-2 border-b border-line p-3">
        <Select className="w-48" aria-label="Status" value={status} onChange={(e) => setStatus(e.target.value)}>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s ? s.replace("_", " ").toLowerCase() : "All statuses"}
            </option>
          ))}
        </Select>
        <Select className="w-44" aria-label="Trigger" value={trigger} onChange={(e) => setTrigger(e.target.value)}>
          {["", "manual", "webhook", "schedule", "api_event", "email", "db_change", "file_uploaded", "app_event", "sub_workflow"].map((t) => (
            <option key={t} value={t}>
              {t || "All triggers"}
            </option>
          ))}
        </Select>
        <Input className="w-72" placeholder="Correlation key (e.g. customer email)" value={correlation} onChange={(e) => setCorrelation(e.target.value)} aria-label="Correlation key" />
      </div>
      {!list.data ? (
        <Spinner />
      ) : list.data.items.length === 0 ? (
        <EmptyState title="No executions match" />
      ) : (
        <Table head={["Status", "Workflow", "Version", "Trigger", "Correlation", "Error", "Duration", "Started"]}>
          {list.data.items.map((e) => (
            <tr key={e.id} className="hover:bg-surface-2">
              <td className="px-4 py-2"><StatusBadge status={e.status} /></td>
              <td className="px-4 py-2">
                <Link href={`/executions/${e.id}`} className="font-medium hover:text-accent">{e.workflow_name}</Link>
                <div className="font-mono text-[11px] text-muted">{e.id.slice(0, 8)}</div>
              </td>
              <td className="px-4 py-2 text-ink-2">v{e.workflow_version}</td>
              <td className="px-4 py-2 text-ink-2">{e.trigger_type}</td>
              <td className="max-w-48 truncate px-4 py-2 text-ink-2">{e.correlation_key ?? "—"}</td>
              <td className="max-w-64 truncate px-4 py-2 text-critical">{e.error?.message ?? ""}</td>
              <td className="px-4 py-2 tabular">{duration(e.duration_ms)}</td>
              <td className="px-4 py-2 text-ink-2">{ago(e.created_at)}</td>
            </tr>
          ))}
        </Table>
      )}
      {list.data && <div className="border-t border-line px-4 py-2 text-xs text-muted">{list.data.total.toLocaleString()} matching executions</div>}
    </Card>
  );
}
