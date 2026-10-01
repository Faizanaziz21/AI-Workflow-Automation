"use client";

import { Plus, Search } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Badge, Button, Card, EmptyState, ErrorNote, Field, Input, Modal, PageHeader, Select, Spinner, StatusBadge, Table } from "@/components/ui";
import { api } from "@/lib/api";
import { useAuth, useData } from "@/lib/auth";
import { ago } from "@/lib/format";
import type { Workflow } from "@/lib/types";

export default function WorkflowsPage() {
  const { workspace, can } = useAuth();
  const router = useRouter();
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("active");
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const list = useData<{ items: Workflow[]; total: number }>(workspace ? `/workspaces/${workspace.id}/workflows` : null, { q, status, limit: 100 });

  return (
    <div className="mx-auto max-w-7xl">
      <PageHeader
        title="Workflows"
        subtitle={list.data ? `${list.data.total} workflows in ${workspace?.name}` : undefined}
        actions={
          <>
            <Link href="/templates">
              <Button>Browse templates</Button>
            </Link>
            {can("workflows:write") && (
              <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setCreating(true)}>
                New workflow
              </Button>
            )}
          </>
        }
      />
      <Card>
        <div className="flex flex-wrap gap-2 border-b border-line p-3">
          <div className="relative w-72">
            <Search className="absolute left-2.5 top-2.5 size-4 text-muted" aria-hidden />
            <Input className="pl-8" placeholder="Search workflows" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search workflows" />
          </div>
          <Select className="w-40" value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
            <option value="active">Active</option>
            <option value="archived">Archived</option>
            <option value="all">All</option>
          </Select>
        </div>
        {!list.data ? (
          <Spinner />
        ) : list.data.items.length === 0 ? (
          <EmptyState title="No workflows yet" body="Create one from scratch or install an enterprise template." action={<Link href="/templates"><Button variant="primary">Open template marketplace</Button></Link>} />
        ) : (
          <Table head={["Name", "Trigger", "Version", "Runs (24h)", "Tags", "Updated"]}>
            {list.data.items.map((w) => (
              <tr key={w.id} className="hover:bg-surface-2">
                <td className="px-4 py-2.5">
                  <Link href={`/workflows/${w.id}`} className="font-medium text-ink hover:text-accent">
                    {w.name}
                  </Link>
                  <div className="max-w-md truncate text-xs text-muted">{w.description}</div>
                </td>
                <td className="px-4 py-2.5 text-ink-2">{w.trigger_type ?? "—"}</td>
                <td className="px-4 py-2.5">
                  <div className="flex items-center gap-1.5">
                    {w.published_version ? <StatusBadge status="published" /> : <StatusBadge status="draft" />}
                    {w.published_version && <Badge>v{w.published_version}</Badge>}
                    {w.has_draft && w.published_version && <Badge>draft</Badge>}
                  </div>
                </td>
                <td className="px-4 py-2.5 tabular">{w.executions_24h ?? 0}</td>
                <td className="px-4 py-2.5">
                  <div className="flex flex-wrap gap-1">{w.tags.slice(0, 3).map((t) => <Badge key={t}>{t}</Badge>)}</div>
                </td>
                <td className="px-4 py-2.5 text-ink-2">{ago(w.updated_at)}</td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
      <Modal
        open={creating}
        onClose={() => setCreating(false)}
        title="New workflow"
        footer={
          <>
            <Button onClick={() => setCreating(false)}>Cancel</Button>
            <Button
              variant="primary"
              disabled={!name.trim()}
              onClick={async () => {
                try {
                  const w = await api<Workflow>(`/workspaces/${workspace!.id}/workflows`, { method: "POST", body: { name } });
                  router.push(`/workflows/${w.id}`);
                } catch (e) {
                  setError((e as Error).message);
                }
              }}
            >
              Create
            </Button>
          </>
        }
      >
        <ErrorNote message={error} />
        <Field label="Name">
          <Input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Refund approval" />
        </Field>
      </Modal>
    </div>
  );
}
