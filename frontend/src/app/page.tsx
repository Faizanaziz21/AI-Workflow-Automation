"use client";

import Link from "next/link";
import { useState } from "react";
import { AICostChart, ExecutionsChart, StatTile } from "@/components/charts";
import { Card, EmptyState, PageHeader, Select, Spinner, StatusBadge, Table } from "@/components/ui";
import { useAuth, useData } from "@/lib/auth";
import { ago, compact, duration, money, pct } from "@/lib/format";
import type { ExecutionSummary } from "@/lib/types";

type Summary = {
  executions: { running: number; waiting: number; waiting_approval: number; started: number; completed: number; failed: number; failure_rate: number; avg_duration_ms: number; p95_duration_ms: number; per_minute: number };
  queue: { ready: number; scheduled: number; in_flight: number; oldest_ready_age_seconds: number };
  ai: { calls: number; cost_usd: number; total_tokens: number; errors: number; fallbacks: number };
  integrations: { calls: number; failures: number; errored_attempts: number };
  retries: { scheduled: number };
  approvals: { pending: number };
};
type Series = { bucket: string; executions: { t: string; started: number; completed: number; failed: number }[]; ai: { t: string; cost_usd: number; tokens: number }[] };
type WfStat = { workflow_id: string; name: string; runs: number; failed: number; failure_rate: number; avg_duration_ms: number; last_run: string };
type NodeStat = { node_type: string; runs: number; failed: number; retries: number; avg_duration_ms: number; p95_duration_ms: number };

const WINDOWS = [
  ["1h", "Last hour"],
  ["24h", "Last 24 hours"],
  ["7d", "Last 7 days"],
  ["30d", "Last 30 days"],
] as const;

export default function DashboardPage() {
  const { workspace } = useAuth();
  const [window, setWindow] = useState<string>("24h");
  const q = { window, workspace_id: workspace?.id };
  const summary = useData<Summary>("/dashboard/summary", q, 10000);
  const series = useData<Series>("/dashboard/timeseries", q, 30000);
  const workflows = useData<WfStat[]>("/dashboard/workflows", q, 30000);
  const nodes = useData<NodeStat[]>("/dashboard/nodes", q, 30000);
  const recent = useData<{ items: ExecutionSummary[] }>("/executions", { workspace_id: workspace?.id, limit: 8 }, 10000);
  const s = summary.data;

  return (
    <div className="mx-auto max-w-7xl">
      <PageHeader
        title="Operations dashboard"
        subtitle={workspace ? `Live health of ${workspace.name}` : undefined}
        actions={
          <Select aria-label="Time range" className="h-8 w-40" value={window} onChange={(e) => setWindow(e.target.value)}>
            {WINDOWS.map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </Select>
        }
      />
      {!s ? (
        <Spinner />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
            <StatTile label="Running now" value={compact(s.executions.running)} sub={`${s.executions.waiting} waiting · ${s.executions.waiting_approval} on approval`} />
            <StatTile label="Completed" value={compact(s.executions.completed)} sub={`${compact(s.executions.started)} started`} />
            <StatTile label="Failure rate" value={pct(s.executions.failure_rate)} sub={`${s.executions.failed} failed`} />
            <StatTile label="Avg duration" value={duration(s.executions.avg_duration_ms)} sub={`p95 ${duration(s.executions.p95_duration_ms)}`} />
            <StatTile label="Executions / min" value={s.executions.per_minute.toFixed(2)} sub={`Queue: ${s.queue.ready} ready · ${s.queue.in_flight} in flight`} />
            <StatTile label="Queue depth" value={compact(s.queue.ready + s.queue.scheduled)} sub={`oldest ready ${s.queue.oldest_ready_age_seconds}s · ${s.queue.scheduled} timers`} />
            <StatTile label="AI calls" value={compact(s.ai.calls)} sub={`${s.ai.errors} errors · ${s.ai.fallbacks} fallbacks`} />
            <StatTile label="AI cost" value={money(s.ai.cost_usd)} sub={`${compact(s.ai.total_tokens)} tokens`} />
            <StatTile label="Integration failures" value={compact(s.integrations.failures)} sub={`${compact(s.integrations.calls)} integration calls`} />
            <StatTile label="Retries scheduled" value={compact(s.retries.scheduled)} sub={`${s.integrations.errored_attempts} errored attempts`} />
            <StatTile label="Pending approvals" value={compact(s.approvals.pending)} sub={<Link className="text-accent hover:underline" href="/approvals">Open inbox</Link>} />
            <StatTile label="Started" value={compact(s.executions.started)} sub={WINDOWS.find(([v]) => v === window)?.[1]} />
          </div>

          <div className="mt-4 grid gap-4 lg:grid-cols-2">
            <Card title="Executions">
              <div className="p-3">{series.data ? <ExecutionsChart data={series.data.executions} bucket={series.data.bucket} /> : <Spinner />}</div>
            </Card>
            <Card title="AI cost">
              <div className="p-3">{series.data ? <AICostChart data={series.data.ai} bucket={series.data.bucket} /> : <Spinner />}</div>
            </Card>
          </div>

          <div className="mt-4 grid gap-4 lg:grid-cols-2">
            <Card title="Workflows">
              <Table head={["Workflow", "Runs", "Failure rate", "Avg duration", "Last run"]} empty={workflows.data?.length === 0}>
                {workflows.data?.map((w) => (
                  <tr key={w.workflow_id}>
                    <td className="px-4 py-2">
                      <Link href={`/workflows/${w.workflow_id}`} className="font-medium text-ink hover:text-accent">
                        {w.name}
                      </Link>
                    </td>
                    <td className="px-4 py-2 tabular">{w.runs}</td>
                    <td className="px-4 py-2 tabular">{pct(w.failure_rate)}</td>
                    <td className="px-4 py-2 tabular">{duration(w.avg_duration_ms)}</td>
                    <td className="px-4 py-2 text-ink-2">{ago(w.last_run)}</td>
                  </tr>
                ))}
              </Table>
            </Card>
            <Card title="Slowest node types (p95)">
              <Table head={["Node type", "Runs", "Failed", "Retries", "p95"]} empty={nodes.data?.length === 0}>
                {nodes.data?.map((n) => (
                  <tr key={n.node_type}>
                    <td className="px-4 py-2 font-mono text-xs">{n.node_type}</td>
                    <td className="px-4 py-2 tabular">{n.runs}</td>
                    <td className="px-4 py-2 tabular">{n.failed}</td>
                    <td className="px-4 py-2 tabular">{n.retries}</td>
                    <td className="px-4 py-2 tabular">{duration(n.p95_duration_ms)}</td>
                  </tr>
                ))}
              </Table>
            </Card>
          </div>

          <Card title="Recent executions" className="mt-4" actions={<Link href="/executions" className="text-xs text-accent hover:underline">View all</Link>}>
            {recent.data?.items.length === 0 ? (
              <EmptyState title="No executions yet" body="Run a workflow or install a template to get started." />
            ) : (
              <Table head={["Status", "Workflow", "Trigger", "Correlation", "Duration", "Started"]}>
                {recent.data?.items.map((e) => (
                  <tr key={e.id} className="hover:bg-surface-2">
                    <td className="px-4 py-2"><StatusBadge status={e.status} /></td>
                    <td className="px-4 py-2"><Link href={`/executions/${e.id}`} className="font-medium hover:text-accent">{e.workflow_name}</Link></td>
                    <td className="px-4 py-2 text-ink-2">{e.trigger_type}</td>
                    <td className="max-w-48 truncate px-4 py-2 text-ink-2">{e.correlation_key ?? "—"}</td>
                    <td className="px-4 py-2 tabular">{duration(e.duration_ms)}</td>
                    <td className="px-4 py-2 text-ink-2">{ago(e.created_at)}</td>
                  </tr>
                ))}
              </Table>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
