"use client";

import Link from "next/link";
import { useState } from "react";
import { Card, EmptyState, Input, PageHeader, Select, Spinner, StatusBadge, Table } from "@/components/ui";
import { useAuth, useData } from "@/lib/auth";
import { ago, duration } from "@/lib/format";
import type { ExecutionSummary } from "@/lib/types";

const STATUSES = ["", "RUNNING", "WAITING", "WAITING_APPROVAL", "RETRYING", "PAUSED", "COMPLETED", "FAILED", "CANCELLED"];

export default function ExecutionsPage() {
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
    <div className="mx-auto max-w-7xl">
      <PageHeader title="Executions" subtitle={list.data ? `${list.data.total.toLocaleString()} matching executions` : undefined} />
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
            {["", "manual", "webhook", "schedule", "api_event", "email", "db_change", "file_uploaded", "sub_workflow"].map((t) => (
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
      </Card>
    </div>
  );
}
