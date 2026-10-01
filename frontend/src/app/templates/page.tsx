"use client";

import { Star } from "lucide-react";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { Badge, Button, Card, ErrorNote, Field, Input, Modal, PageHeader, Select, Spinner } from "@/components/ui";
import { api } from "@/lib/api";
import { useAuth, useData } from "@/lib/auth";
import { useCatalog } from "@/lib/catalog";
import type { ConnectorInfo, Template } from "@/lib/types";

function matches(filter: string, c: ConnectorInfo) {
  if (filter.startsWith("category:")) return c.category === filter.slice(9);
  if (filter.startsWith("capability:")) return c.actions.some((a) => a.capability === filter.slice(11));
  return c.key === filter;
}

export default function TemplatesPage() {
  const { workspace, can } = useAuth();
  const templates = useData<Template[]>("/templates");
  const [category, setCategory] = useState("");
  const [installing, setInstalling] = useState<Template | null>(null);
  const categories = useMemo(() => [...new Set(templates.data?.map((t) => t.category) ?? [])].sort(), [templates.data]);
  const shown = templates.data?.filter((t) => !category || t.category === category) ?? [];

  return (
    <div className="mx-auto max-w-7xl">
      <PageHeader
        title="Template marketplace"
        subtitle="Production-ready enterprise workflows. Install, map your connections, publish."
        actions={
          <Select aria-label="Category" className="w-48" value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="">All categories</option>
            {categories.map((c) => <option key={c}>{c}</option>)}
          </Select>
        }
      />
      {!templates.data ? (
        <Spinner />
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {shown.map((t) => (
            <Card key={t.slug} className="flex flex-col">
              <div className="flex-1 p-4">
                <div className="flex items-start justify-between gap-2">
                  <h2 className="text-sm font-semibold">{t.name}</h2>
                  {t.featured && <span className="flex items-center gap-1 text-xs text-ink-2"><Star className="size-3.5 fill-warning text-warning" aria-hidden />Flagship</span>}
                </div>
                <div className="mt-1 text-xs text-muted">{t.category} · {t.node_count} nodes · {t.install_count} installs</div>
                <p className="mt-2 text-sm text-ink-2">{t.summary}</p>
                <div className="mt-3 flex flex-wrap gap-1">{t.tags.map((tag) => <Badge key={tag}>{tag}</Badge>)}</div>
              </div>
              <div className="flex items-center justify-between border-t border-line px-4 py-3">
                <span className="text-xs text-muted">{Object.keys(t.connection_roles).length} connections</span>
                {can("workflows:write") && <Button size="sm" variant="primary" onClick={() => setInstalling(t)}>Use template</Button>}
              </div>
            </Card>
          ))}
        </div>
      )}
      {installing && workspace && <InstallModal template={installing} workspaceId={workspace.id} onClose={() => setInstalling(null)} />}
    </div>
  );
}

function InstallModal({ template, workspaceId, onClose }: { template: Template; workspaceId: string; onClose: () => void }) {
  const { connections, connectors } = useCatalog(workspaceId);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [name, setName] = useState(template.name);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const router = useRouter();
  return (
    <Modal
      open
      onClose={onClose}
      title={`Install “${template.name}”`}
      wide
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            loading={busy}
            onClick={async () => {
              setBusy(true);
              try {
                const r = await api<{ workflow_id: string }>(`/templates/${template.slug}/install`, {
                  method: "POST",
                  body: { workspace_id: workspaceId, name, connections: mapping },
                });
                router.push(`/workflows/${r.workflow_id}`);
              } catch (e) {
                setError((e as Error).message);
                setBusy(false);
              }
            }}
          >
            Install as draft
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <ErrorNote message={error} />
        <p className="text-sm text-ink-2">{template.description || template.summary}</p>
        <Field label="Workflow name">
          <Input value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <div className="grid gap-3 sm:grid-cols-2">
          {Object.entries(template.connection_roles).map(([role, filter]) => {
            const keys = new Set(connectors.filter((c) => matches(filter, c)).map((c) => c.key));
            const options = connections.filter((c) => keys.has(c.connector_key));
            return (
              <Field key={role} label={role.replaceAll("_", " ")} hint={filter.replace(":", ": ")}>
                <Select value={mapping[role] ?? ""} onChange={(e) => setMapping({ ...mapping, [role]: e.target.value })}>
                  <option value="">Map later</option>
                  {options.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                </Select>
              </Field>
            );
          })}
        </div>
        <p className="text-xs text-muted">Unmapped connections can be selected in the editor; the validator blocks publishing until every required connection is set.</p>
      </div>
    </Modal>
  );
}
