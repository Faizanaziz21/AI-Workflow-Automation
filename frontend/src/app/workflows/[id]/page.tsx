"use client";

import { ArrowLeft, CheckCircle2, GitCompare, History, Link2, Play, Rocket, Save, ShieldCheck, Undo2 } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { NodeConfigPanel } from "@/components/editor/NodeConfigPanel";
import { NodePalette } from "@/components/editor/NodePalette";
import { WorkflowCanvas } from "@/components/editor/WorkflowCanvas";
import { Badge, Button, Drawer, ErrorNote, Field, Input, JsonView, Modal, Spinner, StatusBadge, Textarea, useToast } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useCatalog, useHandles } from "@/lib/catalog";
import { ago, dateTime } from "@/lib/format";
import type { Definition, NodeDef, NodeTypeInfo, Version, Workflow } from "@/lib/types";

type Issue = { level: "error" | "warning"; message: string; node_id?: string; field?: string };
type Report = { valid: boolean; errors: Issue[]; warnings: Issue[] };

function uniqueId(base: string, existing: Set<string>) {
  const clean = base.replace(/[^A-Za-z0-9_]/g, "_").replace(/^(\d)/, "n_$1").slice(0, 40) || "node";
  let id = clean;
  let i = 2;
  while (existing.has(id)) id = `${clean}_${i++}`;
  return id;
}

export default function WorkflowEditorPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const toast = useToast();
  const { workspace, can } = useAuth();
  const catalog = useCatalog(workspace?.id);
  const [wf, setWf] = useState<Workflow | null>(null);
  const [definition, setDefinition] = useState<Definition | null>(null);
  const [revision, setRevision] = useState<number | null>(null);
  const [dirty, setDirty] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [modal, setModal] = useState<null | "publish" | "run" | "versions" | "trigger" | "settings">(null);
  const canEdit = can("workflows:write") && wf?.status === "active";
  const base = workspace ? `/workspaces/${workspace.id}/workflows/${id}` : null;

  const load = useCallback(async () => {
    if (!base) return;
    try {
      const w = await api<Workflow>(base);
      setWf(w);
      const v = w.draft ?? w.published;
      setDefinition(v?.definition ?? { nodes: [], edges: [], settings: {}, variables: {} });
      setRevision(w.draft?.revision ?? null);
      setDirty(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [base]);

  useEffect(() => {
    load();
  }, [load]);

  const typeIndex = useMemo(() => Object.fromEntries(catalog.nodeTypes.map((t) => [t.type, t])), [catalog.nodeTypes]);

  const handles = useHandles(definition?.nodes);

  const validate = useCallback(
    async (def: Definition | null = definition) => {
      if (!base || !def) return null;
      const r = await api<Report>(`${base}/validate`, { method: "POST", body: { definition: def } });
      setReport(r);
      return r;
    },
    [base, definition],
  );

  const update = useCallback((d: Definition) => {
    setDefinition(d);
    setDirty(true);
  }, []);

  const save = useCallback(async () => {
    if (!base || !definition) return;
    setBusy("save");
    try {
      const v = await api<Version>(`${base}/draft`, { method: "PUT", body: { definition, expected_revision: revision } });
      setRevision(v.revision);
      setDirty(false);
      setWf((w) => (w ? { ...w, has_draft: true, draft: v } : w));
      await validate(v.definition);
      toast("success", `Draft v${v.version} saved`);
    } catch (e) {
      toast("error", e instanceof ApiError && e.code === "revision_conflict" ? "Someone else changed this draft — reload to continue." : String((e as Error).message));
    } finally {
      setBusy(null);
    }
  }, [base, definition, revision, toast, validate]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "s") {
        e.preventDefault();
        if (canEdit) save();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [save, canEdit]);

  useEffect(() => {
    if (definition && !report) validate();
  }, [definition, report, validate]);

  const addNode = useCallback(
    (t: NodeTypeInfo, position?: { x: number; y: number }) => {
      if (!definition) return;
      const ids = new Set(definition.nodes.map((n) => n.id));
      const nodeId = uniqueId(t.type.split(".").pop() ?? "node", ids);
      const last = definition.nodes[definition.nodes.length - 1];
      const node: NodeDef = {
        id: nodeId,
        type: t.type,
        name: t.label,
        config: {},
        position: position ?? { x: (last?.position.x ?? 0) + 300, y: last?.position.y ?? 200 },
      };
      const edges = [...definition.edges];
      if (!position && last && !t.is_trigger) edges.push({ source: last.id, target: nodeId, source_handle: (handles[last.id] ?? ["out"])[0] ?? "out" });
      update({ ...definition, nodes: [...definition.nodes, node], edges });
      setSelected(nodeId);
    },
    [definition, handles, update],
  );

  const issuesByNode = useMemo(() => {
    const out: Record<string, number> = {};
    report?.errors.forEach((i) => i.node_id && (out[i.node_id] = (out[i.node_id] ?? 0) + 1));
    return out;
  }, [report]);

  if (error) return <div className="p-6"><ErrorNote message={error} /></div>;
  if (!wf || !definition || !catalog.ready) return <Spinner label="Loading editor" />;
  const selectedNode = definition.nodes.find((n) => n.id === selected) ?? null;
  const hasTrigger = definition.nodes.some((n) => n.type.startsWith("trigger."));

  return (
    <div className="flex h-full flex-col">
      <div className="flex h-12 shrink-0 items-center justify-between gap-3 border-b border-line bg-surface px-3">
        <div className="flex min-w-0 items-center gap-2">
          <button onClick={() => router.push("/workflows")} className="rounded p-1.5 text-ink-2 hover:bg-surface-2" aria-label="Back">
            <ArrowLeft className="size-4" />
          </button>
          <span className="truncate text-sm font-semibold">{wf.name}</span>
          {wf.published_version && <StatusBadge status="published" />}
          {wf.published_version && <Badge>v{wf.published_version}</Badge>}
          {wf.has_draft && <Badge>draft v{wf.draft?.version} · rev {revision}</Badge>}
          {dirty && <span className="text-xs text-serious">● unsaved</span>}
          {report && (
            <span className={`flex items-center gap-1 text-xs ${report.valid ? "text-good-ink" : "text-critical"}`}>
              {report.valid ? <CheckCircle2 className="size-3.5" /> : null}
              {report.valid ? "Valid" : `${report.errors.length} error(s)`}
              {report.warnings.length > 0 && <span className="text-ink-2"> · {report.warnings.length} warning(s)</span>}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          <Button size="sm" variant="ghost" icon={<ShieldCheck className="size-3.5" />} onClick={() => validate()}>Validate</Button>
          <Button size="sm" variant="ghost" icon={<Link2 className="size-3.5" />} onClick={() => setModal("trigger")}>Trigger</Button>
          <Button size="sm" variant="ghost" icon={<History className="size-3.5" />} onClick={() => setModal("versions")}>Versions</Button>
          <Button size="sm" variant="ghost" onClick={() => setModal("settings")}>Settings</Button>
          {canEdit && <Button size="sm" icon={<Save className="size-3.5" />} loading={busy === "save"} onClick={save} disabled={!dirty}>Save</Button>}
          {can("workflows:publish") && wf.status === "active" && (
            <Button size="sm" icon={<Rocket className="size-3.5" />} onClick={() => setModal("publish")} disabled={dirty || !wf.has_draft}>Publish</Button>
          )}
          {can("executions:run") && (
            <Button size="sm" variant="primary" icon={<Play className="size-3.5" />} onClick={() => setModal("run")} disabled={!wf.published_version}>Run</Button>
          )}
        </div>
      </div>
      <div className="flex min-h-0 flex-1">
        {canEdit && (
          <aside className="w-64 shrink-0 border-r border-line bg-surface">
            <NodePalette types={catalog.nodeTypes} onAdd={(t) => addNode(t)} hasTrigger={hasTrigger} />
          </aside>
        )}
        <div className="relative min-w-0 flex-1">
          <WorkflowCanvas
            definition={definition}
            typeIndex={typeIndex}
            handles={handles}
            issuesByNode={issuesByNode}
            selectedId={selected}
            onSelect={setSelected}
            onChange={canEdit ? update : undefined}
            onDropType={(type, pos) => typeIndex[type] && addNode(typeIndex[type], pos)}
            readOnly={!canEdit}
          />
          {report && (report.errors.length > 0 || report.warnings.length > 0) && (
            <div className="absolute bottom-3 left-3 max-h-40 w-96 overflow-y-auto rounded-md border border-line bg-surface p-2 text-xs shadow-md">
              {[...report.errors, ...report.warnings].map((i, k) => (
                <button key={k} className="block w-full truncate rounded px-1.5 py-1 text-left hover:bg-surface-2" onClick={() => i.node_id && setSelected(i.node_id)}>
                  <span className={i.level === "error" ? "text-critical" : "text-serious"}>{i.level === "error" ? "Error" : "Warning"}</span>
                  {i.node_id && <code className="mx-1 text-ink-2">{i.node_id}</code>}
                  {i.message}
                </button>
              ))}
            </div>
          )}
        </div>
        {selectedNode && (
          <aside className="w-96 shrink-0 border-l border-line bg-surface">
            <NodeConfigPanel
              node={selectedNode}
              info={typeIndex[selectedNode.type]}
              ctx={{ connections: catalog.connections, connectors: catalog.connectors }}
              issues={report?.errors.filter((i) => i.node_id === selectedNode.id) ?? []}
              readOnly={!canEdit}
              onChange={(n) => update({ ...definition, nodes: definition.nodes.map((x) => (x.id === n.id ? n : x)) })}
              onDelete={() => {
                update({
                  ...definition,
                  nodes: definition.nodes.filter((x) => x.id !== selectedNode.id),
                  edges: definition.edges.filter((e) => e.source !== selectedNode.id && e.target !== selectedNode.id),
                });
                setSelected(null);
              }}
            />
          </aside>
        )}
      </div>
      {base && modal === "publish" && <PublishModal base={base} onClose={() => setModal(null)} onDone={load} report={report} />}
      {base && modal === "run" && <RunModal base={base} definition={definition} onClose={() => setModal(null)} />}
      {base && modal === "versions" && <VersionsDrawer base={base} onClose={() => setModal(null)} onRolledBack={load} canPublish={can("workflows:publish")} />}
      {base && modal === "trigger" && <TriggerModal base={base} onClose={() => setModal(null)} canReveal={can("workflows:publish")} />}
      {modal === "settings" && (
        <SettingsModal definition={definition} readOnly={!canEdit} onClose={() => setModal(null)} onChange={(d) => update(d)} />
      )}
    </div>
  );
}

function PublishModal({ base, onClose, onDone, report }: { base: string; onClose: () => void; onDone: () => void; report: Report | null }) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const toast = useToast();
  return (
    <Modal
      open
      onClose={onClose}
      title="Publish new version"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            loading={busy}
            onClick={async () => {
              setBusy(true);
              try {
                const v = await api<Version>(`${base}/publish`, { method: "POST", body: { change_note: note } });
                toast("success", `Version ${v.version} published — new executions use it; running ones keep their version.`);
                onDone();
                onClose();
              } catch (e) {
                setError((e as Error).message);
              } finally {
                setBusy(false);
              }
            }}
          >
            Publish
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <ErrorNote message={error} />
        {report && !report.valid && <ErrorNote message={`${report.errors.length} validation error(s) must be fixed before publishing.`} />}
        <p className="text-sm text-ink-2">Publishing freezes the current draft as an immutable version and activates its trigger. In-flight executions stay pinned to the version they started on.</p>
        <Field label="Change note">
          <Textarea value={note} onChange={(e) => setNote(e.target.value)} placeholder="What changed and why" className="font-sans text-sm" />
        </Field>
      </div>
    </Modal>
  );
}

function RunModal({ base, definition, onClose }: { base: string; definition: Definition; onClose: () => void }) {
  const trigger = definition.nodes.find((n) => n.type.startsWith("trigger."));
  const sample = (trigger?.config?.sample_payload as Record<string, unknown>) ?? {};
  const [input, setInput] = useState(JSON.stringify(trigger?.type === "trigger.webhook" ? { body: sample } : sample, null, 2));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const router = useRouter();
  return (
    <Modal
      open
      onClose={onClose}
      title="Run published version"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            loading={busy}
            onClick={async () => {
              setBusy(true);
              try {
                const payload = JSON.parse(input || "{}");
                const r = await api<{ execution_id: string }>(`${base}/run`, { method: "POST", body: { input: payload } });
                router.push(`/executions/${r.execution_id}`);
              } catch (e) {
                setError((e as Error).message);
                setBusy(false);
              }
            }}
          >
            Start execution
          </Button>
        </>
      }
    >
      <ErrorNote message={error} />
      <Field label="Trigger payload (JSON)" hint="Available to nodes as {{ trigger.* }}">
        <Textarea rows={12} value={input} onChange={(e) => setInput(e.target.value)} />
      </Field>
    </Modal>
  );
}

type Diff = {
  nodes_added: { id: string; type: string }[];
  nodes_removed: { id: string; type: string }[];
  nodes_changed: { id: string; changes: { path: string; before: unknown; after: unknown }[]; moved: boolean }[];
  edges_added: { source: string; target: string; source_handle: string }[];
  edges_removed: { source: string; target: string; source_handle: string }[];
};

function VersionsDrawer({ base, onClose, onRolledBack, canPublish }: { base: string; onClose: () => void; onRolledBack: () => void; canPublish: boolean }) {
  const [versions, setVersions] = useState<Version[] | null>(null);
  const [diff, setDiff] = useState<{ from: number; to: number; data: Diff } | null>(null);
  const toast = useToast();
  useEffect(() => {
    api<Version[]>(`${base}/versions`).then(setVersions);
  }, [base]);
  const latest = versions?.[0]?.version;
  return (
    <Drawer open onClose={onClose} title="Revision history">
      {!versions ? (
        <Spinner />
      ) : (
        <ol className="space-y-2">
          {versions.map((v) => (
            <li key={v.id} className="rounded-md border border-line p-3">
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-semibold">v{v.version}</span>
                  <StatusBadge status={v.status} />
                </div>
                <div className="flex gap-1">
                  {latest && v.version !== latest && (
                    <Button
                      size="sm"
                      variant="ghost"
                      icon={<GitCompare className="size-3.5" />}
                      onClick={async () => setDiff({ from: v.version, to: latest, data: await api<Diff>(`${base}/versions/diff`, { query: { from: v.version, to: latest } }) })}
                    >
                      Compare to v{latest}
                    </Button>
                  )}
                  {canPublish && v.status === "archived" && (
                    <Button
                      size="sm"
                      icon={<Undo2 className="size-3.5" />}
                      onClick={async () => {
                        const nv = await api<Version>(`${base}/rollback`, { method: "POST", body: { version: v.version } });
                        toast("success", `Rolled back: v${v.version} republished as v${nv.version}`);
                        onRolledBack();
                        onClose();
                      }}
                    >
                      Roll back
                    </Button>
                  )}
                </div>
              </div>
              <div className="mt-1 text-xs text-ink-2">
                {v.published_at ? `Published ${dateTime(v.published_at)}` : `Updated ${ago(v.created_at)}`} · rev {v.revision} · <code>{v.definition_hash.slice(0, 10)}</code>
              </div>
              {v.change_note && <p className="mt-1 text-xs text-ink">{v.change_note}</p>}
            </li>
          ))}
        </ol>
      )}
      {diff && (
        <div className="mt-4 rounded-md border border-line p-3 text-xs">
          <div className="mb-2 font-semibold">
            Changes v{diff.from} → v{diff.to}
          </div>
          {diff.data.nodes_added.map((n) => <div key={n.id} className="text-good-ink">+ node {n.id} ({n.type})</div>)}
          {diff.data.nodes_removed.map((n) => <div key={n.id} className="text-critical">− node {n.id} ({n.type})</div>)}
          {diff.data.edges_added.map((e, i) => <div key={i} className="text-good-ink">+ edge {e.source} → {e.target} [{e.source_handle}]</div>)}
          {diff.data.edges_removed.map((e, i) => <div key={i} className="text-critical">− edge {e.source} → {e.target} [{e.source_handle}]</div>)}
          {diff.data.nodes_changed.map((n) => (
            <div key={n.id} className="mt-1">
              <div className="font-medium">~ {n.id}{n.moved ? " (moved)" : ""}</div>
              {n.changes.map((c) => (
                <div key={c.path} className="ml-3 text-ink-2">
                  <code>{c.path}</code>: <span className="text-critical line-through">{JSON.stringify(c.before)}</span> → <span className="text-good-ink">{JSON.stringify(c.after)}</span>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </Drawer>
  );
}

function TriggerModal({ base, onClose, canReveal }: { base: string; onClose: () => void; canReveal: boolean }) {
  const [info, setInfo] = useState<Record<string, unknown> | null>(null);
  const load = (reveal = false) => api<Record<string, unknown>>(`${base}/trigger`, { query: { reveal_secret: reveal } }).then(setInfo);
  const mounted = useRef(false);
  useEffect(() => {
    if (!mounted.current) {
      mounted.current = true;
      load();
    }
  });
  return (
    <Modal open onClose={onClose} title="Trigger" wide>
      {!info ? (
        <Spinner />
      ) : (
        <div className="space-y-3 text-sm">
          {info.webhook_url ? (
            <>
              <Field label="Webhook URL">
                <Input readOnly value={String(info.webhook_url)} onFocus={(e) => e.currentTarget.select()} />
              </Field>
              {info.signing_secret ? (
                <Field label="Signing secret" hint="Send X-FlowForge-Timestamp and X-FlowForge-Signature: v1=HMAC_SHA256(secret, timestamp + '.' + body)">
                  <div className="flex gap-2">
                    <Input readOnly value={String(info.signing_secret)} />
                    {canReveal && <Button onClick={() => load(true)}>Reveal</Button>}
                  </div>
                </Field>
              ) : null}
            </>
          ) : null}
          <JsonView value={Object.fromEntries(Object.entries(info).filter(([k]) => !["webhook_url", "signing_secret"].includes(k)))} />
        </div>
      )}
    </Modal>
  );
}

function SettingsModal({ definition, onClose, onChange, readOnly }: { definition: Definition; onClose: () => void; onChange: (d: Definition) => void; readOnly: boolean }) {
  const [settings, setSettings] = useState(JSON.stringify(definition.settings ?? {}, null, 2));
  const [variables, setVariables] = useState(JSON.stringify(definition.variables ?? {}, null, 2));
  const [error, setError] = useState<string | null>(null);
  return (
    <Modal
      open
      onClose={onClose}
      title="Workflow settings"
      wide
      footer={
        !readOnly && (
          <>
            <Button onClick={onClose}>Cancel</Button>
            <Button
              variant="primary"
              onClick={() => {
                try {
                  onChange({ ...definition, settings: JSON.parse(settings), variables: JSON.parse(variables) });
                  onClose();
                } catch (e) {
                  setError(`Invalid JSON: ${(e as Error).message}`);
                }
              }}
            >
              Apply
            </Button>
          </>
        )
      }
    >
      <div className="space-y-3">
        <ErrorNote message={error} />
        <Field label="Settings" hint="execution_timeout_seconds, max_parallel_nodes, default_retry, mask_fields, …">
          <Textarea rows={8} value={settings} onChange={(e) => setSettings(e.target.value)} readOnly={readOnly} />
        </Field>
        <Field label="Variables (constants available as vars.*)">
          <Textarea rows={6} value={variables} onChange={(e) => setVariables(e.target.value)} readOnly={readOnly} />
        </Field>
        <p className="text-xs text-muted">
          Back to <Link className="text-accent" href="/workflows">all workflows</Link>.
        </p>
      </div>
    </Modal>
  );
}
