"use client";

import { KeyRound, Plug, Plus, RefreshCw } from "lucide-react";
import { useState } from "react";
import { SchemaForm } from "@/components/SchemaForm";
import { Badge, Button, Card, EmptyState, ErrorNote, Field, Input, Modal, PageHeader, Select, Spinner, StatusBadge, Table, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useCatalog } from "@/lib/catalog";
import { ago } from "@/lib/format";
import type { Connection, ConnectorInfo } from "@/lib/types";

export default function ConnectionsPage() {
  const { workspace, can } = useAuth();
  const catalog = useCatalog(workspace?.id);
  const toast = useToast();
  const [creating, setCreating] = useState(false);
  const [rotating, setRotating] = useState<Connection | null>(null);
  const [testing, setTesting] = useState<string | null>(null);
  const connectorByKey = Object.fromEntries(catalog.connectors.map((c) => [c.key, c]));
  const canManage = can("connections:manage");

  return (
    <div className="mx-auto max-w-7xl">
      <PageHeader
        title="Connections"
        subtitle="Credentials are envelope-encrypted (AES-256-GCM) and never returned by the API — only field names are shown."
        actions={canManage && <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setCreating(true)}>New connection</Button>}
      />
      <Card>
        {!catalog.ready ? (
          <Spinner />
        ) : catalog.connections.length === 0 ? (
          <EmptyState title="No connections" body="Connect CRMs, chat, email, databases and AI providers." />
        ) : (
          <Table head={["Name", "Connector", "Status", "Credentials", "Last tested", ""]}>
            {catalog.connections.map((c) => (
              <tr key={c.id}>
                <td className="px-4 py-2.5 font-medium">{c.name}</td>
                <td className="px-4 py-2.5 text-ink-2">
                  {connectorByKey[c.connector_key]?.name ?? c.connector_key} <Badge>{connectorByKey[c.connector_key]?.category}</Badge>
                </td>
                <td className="px-4 py-2.5">
                  <StatusBadge status={c.status} />
                  {c.status_message && <div className="max-w-xs truncate text-xs text-muted" title={c.status_message}>{c.status_message}</div>}
                </td>
                <td className="px-4 py-2.5 text-xs text-ink-2">
                  {c.credential_fields.length ? c.credential_fields.join(", ") : "none"}
                  {c.secret_version && <span className="text-muted"> · v{c.secret_version}</span>}
                </td>
                <td className="px-4 py-2.5 text-ink-2">{ago(c.last_tested_at)}</td>
                <td className="px-4 py-2.5">
                  <div className="flex justify-end gap-1">
                    {c.can_use && (
                      <Button
                        size="sm"
                        icon={<RefreshCw className="size-3.5" />}
                        loading={testing === c.id}
                        onClick={async () => {
                          setTesting(c.id);
                          try {
                            const r = await api<{ ok: boolean; message: string }>(`/workspaces/${workspace!.id}/connections/${c.id}/test`, { method: "POST" });
                            toast(r.ok ? "success" : "error", r.message);
                            catalog.reloadConnections();
                          } finally {
                            setTesting(null);
                          }
                        }}
                      >
                        Test
                      </Button>
                    )}
                    {canManage && <Button size="sm" icon={<KeyRound className="size-3.5" />} onClick={() => setRotating(c)}>Rotate</Button>}
                  </div>
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
      <Card title="Available connectors" className="mt-4">
        <div className="grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-4">
          {catalog.connectors.map((c) => (
            <div key={c.key} className="rounded-md border border-line p-3">
              <div className="flex items-center gap-2 text-sm font-medium"><Plug className="size-4 text-muted" aria-hidden />{c.name}</div>
              <div className="mt-1 text-xs text-ink-2">{c.description}</div>
              <div className="mt-2 text-[11px] text-muted">{c.actions.length} actions · {c.triggers.length} triggers · {c.auth.type}</div>
            </div>
          ))}
        </div>
      </Card>
      {creating && workspace && (
        <ConnectionModal connectors={catalog.connectors} workspaceId={workspace.id} onClose={() => setCreating(false)} onSaved={() => { setCreating(false); catalog.reloadConnections(); }} />
      )}
      {rotating && workspace && (
        <RotateModal connection={rotating} connector={connectorByKey[rotating.connector_key]} workspaceId={workspace.id} onClose={() => setRotating(null)} onSaved={() => { setRotating(null); catalog.reloadConnections(); toast("success", "Credentials rotated"); }} />
      )}
    </div>
  );
}

function ConnectionModal({ connectors, workspaceId, onClose, onSaved }: { connectors: ConnectorInfo[]; workspaceId: string; onClose: () => void; onSaved: () => void }) {
  const [key, setKey] = useState(connectors[0]?.key ?? "");
  const [name, setName] = useState("");
  const [config, setConfig] = useState<Record<string, unknown>>({});
  const [creds, setCreds] = useState<Record<string, unknown>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const connector = connectors.find((c) => c.key === key);
  return (
    <Modal
      open
      onClose={onClose}
      wide
      title="New connection"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            loading={busy}
            disabled={!name}
            onClick={async () => {
              setBusy(true);
              try {
                await api(`/workspaces/${workspaceId}/connections`, { method: "POST", body: { name, connector_key: key, config, credentials: creds } });
                onSaved();
              } catch (e) {
                setError((e as Error).message);
                setBusy(false);
              }
            }}
          >
            Save encrypted
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <ErrorNote message={error} />
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Connector">
            <Select value={key} onChange={(e) => { setKey(e.target.value); setConfig({}); setCreds({}); }}>
              {connectors.map((c) => <option key={c.key} value={c.key}>{c.name} ({c.category})</option>)}
            </Select>
          </Field>
          <Field label="Name">
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Production HubSpot" />
          </Field>
        </div>
        {connector?.auth.description && <p className="text-xs text-ink-2">{connector.auth.description}</p>}
        {connector && (
          <>
            <div className="text-xs font-semibold uppercase tracking-wide text-muted">Configuration</div>
            <SchemaForm schema={connector.auth.config_schema} value={config} onChange={setConfig} ctx={{ connections: [], connectors: [] }} />
            <div className="text-xs font-semibold uppercase tracking-wide text-muted">Credentials (write-only)</div>
            <SchemaForm schema={connector.auth.credentials_schema} value={creds} onChange={setCreds} ctx={{ connections: [], connectors: [] }} />
          </>
        )}
      </div>
    </Modal>
  );
}

function RotateModal({ connection, connector, workspaceId, onClose, onSaved }: { connection: Connection; connector?: ConnectorInfo; workspaceId: string; onClose: () => void; onSaved: () => void }) {
  const [creds, setCreds] = useState<Record<string, unknown>>({});
  const [error, setError] = useState<string | null>(null);
  return (
    <Modal
      open
      onClose={onClose}
      title={`Rotate credentials — ${connection.name}`}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            disabled={Object.keys(creds).length === 0}
            onClick={async () => {
              try {
                await api(`/workspaces/${workspaceId}/connections/${connection.id}/rotate`, { method: "POST", body: { credentials: creds } });
                onSaved();
              } catch (e) {
                setError((e as Error).message);
              }
            }}
          >
            Rotate
          </Button>
        </>
      }
    >
      <ErrorNote message={error} />
      <p className="mb-3 text-sm text-ink-2">Only the fields you fill are replaced. The new values are encrypted with a fresh data key and the secret version is incremented.</p>
      {connector && <SchemaForm schema={connector.auth.credentials_schema} value={creds} onChange={setCreds} ctx={{ connections: [], connectors: [] }} />}
    </Modal>
  );
}
