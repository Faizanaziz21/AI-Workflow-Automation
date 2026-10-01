"use client";

import { Ban, Pause, Play, RotateCcw, Repeat } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useMemo, useState } from "react";
import { WorkflowCanvas, type NodeOverlay } from "@/components/editor/WorkflowCanvas";
import { Badge, Button, Card, Drawer, ErrorNote, JsonView, Spinner, StatusBadge, Tabs, cn, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { useAuth, useData } from "@/lib/auth";
import { useCatalog, useHandles } from "@/lib/catalog";
import { dateTime, duration } from "@/lib/format";
import type { ExecEvent, ExecutionDetail, NodeRun } from "@/lib/types";

const TERMINAL = ["COMPLETED", "FAILED", "CANCELLED"];

export default function ExecutionPage() {
  const { id } = useParams<{ id: string }>();
  const { workspace, can } = useAuth();
  const toast = useToast();
  const catalog = useCatalog(workspace?.id);
  const ex = useData<ExecutionDetail>(`/executions/${id}`, undefined, 3000);
  const events = useData<ExecEvent[]>(`/executions/${id}/events`, undefined, 3000);
  const [selected, setSelected] = useState<string | null>(null);
  const [view, setView] = useState<"graph" | "timeline">("graph");
  const [busy, setBusy] = useState<string | null>(null);
  const handles = useHandles(ex.data?.definition.nodes);
  const typeIndex = useMemo(() => Object.fromEntries(catalog.nodeTypes.map((t) => [t.type, t])), [catalog.nodeTypes]);

  const overlay: NodeOverlay = useMemo(() => {
    const o: NodeOverlay = {};
    for (const r of ex.data?.node_runs ?? []) {
      if (r.scope) continue; // root scope shown on the graph; iterations in the node drawer
      o[r.node_id] = { status: r.status, durationMs: r.duration_ms, attempts: r.attempt };
    }
    return o;
  }, [ex.data]);

  if (ex.error) return <ErrorNote message={ex.error} />;
  if (!ex.data) return <Spinner />;
  const e = ex.data;
  const terminal = TERMINAL.includes(e.status);
  const canOperate = can("executions:operate");

  async function act(action: string, path = `/executions/${id}/${action}`, body?: unknown) {
    setBusy(action);
    try {
      await api(path, { method: "POST", body });
      toast("success", `${action} requested`);
      ex.reload();
      events.reload();
    } catch (err) {
      toast("error", (err as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="mx-auto max-w-[1600px]">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-xl font-semibold">{e.workflow_name}</h1>
            <StatusBadge status={e.status} />
            <Badge>v{e.workflow_version}</Badge>
            {e.retry_count > 0 && <Badge>retried ×{e.retry_count}</Badge>}
          </div>
          <div className="mt-1 text-xs text-ink-2">
            <code>{e.id}</code> · {e.trigger_type} · started {dateTime(e.created_at)} · {duration(e.duration_ms)}
            {e.correlation_key && <> · correlation <code>{e.correlation_key}</code></>}
            {" · "}
            <Link href={`/workflows/${e.workflow_id}`} className="text-accent hover:underline">open workflow</Link>
          </div>
        </div>
        {canOperate && (
          <div className="flex gap-2">
            {!terminal && !e.is_paused && <Button size="sm" icon={<Pause className="size-3.5" />} loading={busy === "pause"} onClick={() => act("pause")}>Pause</Button>}
            {!terminal && e.is_paused && <Button size="sm" icon={<Play className="size-3.5" />} loading={busy === "resume"} onClick={() => act("resume")}>Resume</Button>}
            {(e.status === "FAILED" || e.status === "CANCELLED") && (
              <Button size="sm" variant="primary" icon={<RotateCcw className="size-3.5" />} loading={busy === "retry"} onClick={() => act("retry")}>
                Retry failed nodes
              </Button>
            )}
            {!terminal && <Button size="sm" variant="danger" icon={<Ban className="size-3.5" />} loading={busy === "cancel"} onClick={() => act("cancel")}>Cancel</Button>}
          </div>
        )}
      </div>
      {e.error && <div className="mb-3"><ErrorNote message={`${e.error.node_id ? `[${e.error.node_id}] ` : ""}${e.error.message}`} /></div>}
      {!e.sensitive_data_visible && (
        <p className="mb-3 text-xs text-muted">Sensitive fields are masked for your role.</p>
      )}
      <Tabs value={view} onChange={setView} tabs={[{ id: "graph", label: "Graph" }, { id: "timeline", label: "Timeline" }]} />
      <div className="mt-3 grid gap-4 lg:grid-cols-[1fr_380px]">
        {view === "graph" ? (
          <Card className="h-[560px] overflow-hidden">
            <WorkflowCanvas
              definition={{ nodes: e.definition.nodes, edges: e.definition.edges, settings: {}, variables: {} }}
              typeIndex={typeIndex}
              handles={handles}
              overlay={overlay}
              selectedId={selected}
              onSelect={setSelected}
              readOnly
            />
          </Card>
        ) : (
          <Card>
            <Timeline runs={e.node_runs} onSelect={setSelected} names={Object.fromEntries(e.definition.nodes.map((n) => [n.id, n.name]))} />
          </Card>
        )}
        <Card title="Event log" className="flex h-[560px] flex-col overflow-hidden">
          <ol className="min-h-0 flex-1 overflow-y-auto text-xs">
            {(events.data ?? []).map((ev) => (
              <li key={ev.id} className="border-b border-line px-3 py-2">
                <div className="flex items-center justify-between gap-2">
                  <span className={cn("font-medium", ev.level === "error" ? "text-critical" : ev.level === "warning" ? "text-serious" : "text-ink")}>
                    {ev.type.replaceAll("_", " ")}
                  </span>
                  <span className="text-muted tabular">{new Date(ev.created_at).toLocaleTimeString()}</span>
                </div>
                {ev.node_id && (
                  <button className="font-mono text-[11px] text-accent hover:underline" onClick={() => setSelected(ev.node_id)}>
                    {ev.node_id}
                    {ev.scope ? ` @ ${ev.scope}` : ""}
                  </button>
                )}
                {ev.message && <div className="text-ink-2">{ev.message}</div>}
              </li>
            ))}
          </ol>
        </Card>
      </div>
      <Card title="Trigger payload" className="mt-4">
        <div className="p-3"><JsonView value={e.trigger_payload} /></div>
      </Card>
      {selected && (
        <NodeDrawer
          executionId={id}
          nodeId={selected}
          name={e.definition.nodes.find((n) => n.id === selected)?.name ?? selected}
          canReplay={canOperate && terminal}
          onReplay={(scope) => act("replay", `/executions/${id}/nodes/${selected}/replay`, { scope })}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  );
}

function Timeline({ runs, onSelect, names }: { runs: NodeRun[]; onSelect: (id: string) => void; names: Record<string, string> }) {
  const shown = runs.filter((r) => r.status !== "SKIPPED");
  return (
    <ol className="divide-y divide-[var(--border)]">
      {shown.map((r) => (
        <li key={r.id}>
          <button className="flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-surface-2" onClick={() => onSelect(r.node_id)}>
            <StatusBadge status={r.status} className="w-40 justify-start" />
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm font-medium">{names[r.node_id] ?? r.node_id}</div>
              <div className="truncate text-[11px] text-muted">
                {r.node_type}
                {r.scope && ` · iteration ${r.scope}`}
                {r.attempt > 1 && ` · attempt ${r.attempt}/${r.max_attempts}`}
                {r.wait_until && r.status.startsWith("WAITING") && ` · until ${dateTime(r.wait_until)}`}
              </div>
              {r.error?.message && <div className="truncate text-xs text-critical">{r.error.message}</div>}
            </div>
            <span className="text-sm tabular text-ink-2">{duration(r.duration_ms)}</span>
          </button>
        </li>
      ))}
    </ol>
  );
}

function NodeDrawer({ executionId, nodeId, name, canReplay, onReplay, onClose }: { executionId: string; nodeId: string; name: string; canReplay: boolean; onReplay: (scope: string) => void; onClose: () => void }) {
  const runs = useData<NodeRun[]>(`/executions/${executionId}/nodes`, { node_id: nodeId }, 3000);
  const logs = useData<ExecEvent[]>(`/executions/${executionId}/events`, { node_id: nodeId }, 3000);
  const [idx, setIdx] = useState(0);
  const [tab, setTab] = useState<"output" | "input" | "error" | "logs">("output");
  const run = runs.data?.[Math.min(idx, (runs.data?.length ?? 1) - 1)];
  return (
    <Drawer
      open
      onClose={onClose}
      title={<span>{name} <code className="text-xs font-normal text-muted">{nodeId}</code></span>}
      actions={canReplay && run && <Button size="sm" icon={<Repeat className="size-3.5" />} onClick={() => onReplay(run.scope)}>Replay from here</Button>}
    >
      {!runs.data ? (
        <Spinner />
      ) : !run ? (
        <p className="text-sm text-ink-2">This node has not run in this execution.</p>
      ) : (
        <div className="space-y-3">
          {runs.data.length > 1 && (
            <select className="h-8 w-full rounded-md border border-line bg-surface px-2 text-sm" value={idx} onChange={(e) => setIdx(Number(e.target.value))} aria-label="Iteration">
              {runs.data.map((r, i) => (
                <option key={r.id} value={i}>
                  {r.scope ? `Iteration ${r.scope}` : "Main"} — {r.status}
                </option>
              ))}
            </select>
          )}
          <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
            <dt className="text-muted">Status</dt><dd><StatusBadge status={run.status} /></dd>
            <dt className="text-muted">Type</dt><dd className="font-mono">{run.node_type}</dd>
            <dt className="text-muted">Attempts</dt><dd>{run.attempt} / {run.max_attempts}</dd>
            <dt className="text-muted">Duration</dt><dd>{duration(run.duration_ms)}</dd>
            <dt className="text-muted">Started</dt><dd>{dateTime(run.started_at)}</dd>
            <dt className="text-muted">Branches</dt><dd>{run.branches?.join(", ") ?? "—"}</dd>
            <dt className="text-muted">Idempotency key</dt><dd className="truncate font-mono">{run.idempotency_key}</dd>
          </dl>
          <Tabs value={tab} onChange={setTab} tabs={[{ id: "output", label: "Output" }, { id: "input", label: "Input" }, { id: "error", label: "Error" }, { id: "logs", label: `Logs (${logs.data?.length ?? 0})` }]} />
          {tab === "output" && <JsonView value={run.output} />}
          {tab === "input" && <JsonView value={run.input} />}
          {tab === "error" && (run.error ? <JsonView value={run.error} /> : <p className="text-sm text-ink-2">No error.</p>)}
          {tab === "logs" && (
            <ol className="space-y-1 text-xs">
              {logs.data?.map((l) => (
                <li key={l.id} className="rounded bg-surface-2 px-2 py-1.5">
                  <span className="text-muted tabular">{new Date(l.created_at).toLocaleTimeString()}</span>{" "}
                  <span className={l.level === "error" ? "text-critical" : l.level === "warning" ? "text-serious" : ""}>{l.type}</span> {l.message}
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
    </Drawer>
  );
}
