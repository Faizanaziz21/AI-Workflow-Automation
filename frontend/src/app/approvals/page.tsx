"use client";

import { Check, MessageSquare, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { SchemaForm } from "@/components/SchemaForm";
import { Badge, Button, Card, EmptyState, ErrorNote, Field, JsonView, PageHeader, Select, Spinner, StatusBadge, Textarea, cn, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { useAuth, useData } from "@/lib/auth";
import { ago, dateTime } from "@/lib/format";
import type { Approval, JsonSchema } from "@/lib/types";

export default function ApprovalsPage() {
  const { workspace } = useAuth();
  const [status, setStatus] = useState("pending");
  const list = useData<{ items: Approval[]; total: number }>("/approvals", { status, workspace_id: workspace?.id, limit: 100 }, 5000);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  useEffect(() => {
    if (!selectedId && list.data?.items.length) setSelectedId(list.data.items[0].id);
  }, [list.data, selectedId]);

  return (
    <div className="mx-auto max-w-7xl">
      <PageHeader
        title="Approval inbox"
        subtitle="Human-in-the-loop decisions. Workflows resume from exactly this step once decided."
        actions={
          <Select aria-label="Status" className="w-40" value={status} onChange={(e) => { setStatus(e.target.value); setSelectedId(null); }}>
            <option value="pending">Pending</option>
            <option value="approved">Approved</option>
            <option value="rejected">Rejected</option>
            <option value="expired">Expired</option>
            <option value="all">All</option>
          </Select>
        }
      />
      <div className="grid gap-4 lg:grid-cols-[380px_1fr]">
        <Card className="max-h-[75vh] overflow-y-auto">
          {!list.data ? (
            <Spinner />
          ) : list.data.items.length === 0 ? (
            <EmptyState title="Inbox zero" body="No approvals in this view." />
          ) : (
            <ul className="divide-y divide-[var(--border)]">
              {list.data.items.map((a) => {
                const overdue = a.status === "pending" && a.due_at && new Date(a.due_at) < new Date();
                return (
                  <li key={a.id}>
                    <button onClick={() => setSelectedId(a.id)} className={cn("block w-full px-4 py-3 text-left hover:bg-surface-2", selectedId === a.id && "bg-accent-soft")}>
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate text-sm font-medium">{a.title}</span>
                        <StatusBadge status={a.status} />
                      </div>
                      <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-ink-2">
                        <span>{a.workflow_name}</span>
                        <Badge>{a.kind.replace("_", " ")}</Badge>
                        {a.escalation_level > 0 && <Badge className="text-serious">escalated L{a.escalation_level}</Badge>}
                        <span className={overdue ? "text-critical" : ""}>{a.status === "pending" ? `due ${ago(a.due_at)}` : ago(a.created_at)}</span>
                      </div>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </Card>
        {selectedId ? <ApprovalDetail id={selectedId} onDecided={() => list.reload()} /> : <Card><EmptyState title="Select a request" /></Card>}
      </div>
    </div>
  );
}

function ApprovalDetail({ id, onDecided }: { id: string; onDecided: () => void }) {
  const detail = useData<Approval>(`/approvals/${id}`);
  const toast = useToast();
  const [comment, setComment] = useState("");
  const [response, setResponse] = useState<Record<string, unknown>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setResponse((detail.data?.response_data as Record<string, unknown>) ?? {});
    setError(null);
  }, [detail.data]);
  if (!detail.data) return <Card><Spinner /></Card>;
  const a = detail.data;

  async function decide(decision: "approve" | "reject") {
    setBusy(decision);
    setError(null);
    try {
      await api(`/approvals/${id}/${decision}`, { method: "POST", body: { comment: comment || null, response_data: a.form_schema || a.kind === "manual_review" ? response : undefined } });
      toast("success", decision === "approve" ? "Approved — workflow resuming" : "Rejected — workflow resuming on the rejection path");
      setComment("");
      detail.reload();
      onDecided();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const editableSchema: JsonSchema | null =
    a.form_schema ?? (a.kind === "manual_review" && a.response_data
      ? { type: "object", properties: Object.fromEntries(Object.keys(a.response_data).map((k) => [k, { type: "string", "x-multiline": true }])) }
      : null);

  return (
    <Card
      title={a.title}
      actions={
        <Link href={`/executions/${a.execution_id}`} className="text-xs text-accent hover:underline">
          View execution
        </Link>
      }
    >
      <div className="space-y-4 p-4">
        <div className="flex flex-wrap gap-2 text-xs text-ink-2">
          <StatusBadge status={a.status} />
          <Badge>{a.kind.replace("_", " ")}</Badge>
          {a.required_approvals > 1 && <Badge>quorum {a.approvals_count}/{a.required_approvals}</Badge>}
          <span>Requested {dateTime(a.created_at)}</span>
          {a.due_at && <span>· due {dateTime(a.due_at)}</span>}
        </div>
        {a.description && <p className="whitespace-pre-wrap text-sm text-ink">{a.description}</p>}
        <div>
          <div className="mb-1 text-xs font-medium text-ink-2">Context</div>
          <JsonView value={a.context} />
        </div>
        {editableSchema && a.status === "pending" && (
          <div className="rounded-md border border-line p-3">
            <div className="mb-2 text-xs font-medium text-ink-2">{a.kind === "manual_review" ? "Edit before approving" : "Requested information"}</div>
            <SchemaForm schema={editableSchema} value={response} onChange={setResponse} ctx={{ connections: [], connectors: [] }} />
          </div>
        )}
        <ErrorNote message={error} />
        {a.status === "pending" && (
          a.can_decide ? (
            <div className="space-y-2">
              <Field label="Comment">
                <Textarea className="font-sans text-sm" value={comment} onChange={(e) => setComment(e.target.value)} placeholder="Reason for your decision (recorded in the audit trail)" />
              </Field>
              <div className="flex flex-wrap gap-2">
                <Button variant="primary" icon={<Check className="size-4" />} loading={busy === "approve"} onClick={() => decide("approve")}>
                  {a.kind === "request_info" ? "Submit" : "Approve"}
                </Button>
                {a.kind !== "request_info" && <Button variant="danger" icon={<X className="size-4" />} loading={busy === "reject"} onClick={() => decide("reject")}>Reject</Button>}
                <Button
                  icon={<MessageSquare className="size-4" />}
                  disabled={!comment}
                  onClick={async () => {
                    await api(`/approvals/${id}/comment`, { method: "POST", body: { body: comment } });
                    setComment("");
                    detail.reload();
                  }}
                >
                  Comment only
                </Button>
              </div>
            </div>
          ) : (
            <p className="text-sm text-ink-2">You can view this request but are not one of its approvers.</p>
          )
        )}
        <div>
          <div className="mb-1 text-xs font-medium text-ink-2">History</div>
          <ol className="space-y-1.5 border-l border-line pl-3 text-xs">
            {a.history?.map((h, i) => (
              <li key={i}>
                <span className="font-medium">{h.action}</span> {h.actor_email && <>by {h.actor_email}</>} · <span className="text-muted">{dateTime(h.created_at)}</span>
                {h.comment && <div className="text-ink-2">“{h.comment}”</div>}
              </li>
            ))}
          </ol>
        </div>
      </div>
    </Card>
  );
}
