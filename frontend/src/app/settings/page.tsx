"use client";

import { useState } from "react";
import { Badge, Button, Card, ErrorNote, Field, Input, Modal, PageHeader, Select, Spinner, Table, Tabs, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { useAuth, useData } from "@/lib/auth";
import { ago } from "@/lib/format";

type User = { id: string; email: string; full_name: string; is_active: boolean; last_login_at: string | null; role_assignments: { id: string; role: string; workspace_id: string | null }[] };
type Team = { id: string; name: string; description: string; member_ids: string[] };
type ApiKey = { id: string; name: string; prefix: string; role: string; workspace_id: string | null; last_used_at: string | null; revoked_at: string | null; created_at: string };
const ROLES = ["viewer", "approver", "operator", "workflow_developer", "org_admin"];

export default function SettingsPage() {
  const { can, workspaces } = useAuth();
  const [tab, setTab] = useState<"users" | "teams" | "keys">("users");
  return (
    <div className="mx-auto max-w-6xl">
      <PageHeader title="Organization settings" subtitle="Users, role-based access (org or workspace scope), teams and API keys." />
      <Tabs value={tab} onChange={setTab} tabs={[{ id: "users", label: "Users & roles" }, { id: "teams", label: "Teams" }, ...(can("api_keys:manage") ? [{ id: "keys" as const, label: "API keys" }] : [])]} />
      <div className="mt-4">
        {tab === "users" && <Users canManage={can("users:manage")} workspaces={workspaces} />}
        {tab === "teams" && <Teams canManage={can("teams:manage")} />}
        {tab === "keys" && <Keys workspaces={workspaces} />}
      </div>
    </div>
  );
}

function Users({ canManage, workspaces }: { canManage: boolean; workspaces: { id: string; name: string }[] }) {
  const users = useData<{ items: User[] }>("/users", { limit: 200 });
  const toast = useToast();
  const [form, setForm] = useState<{ email: string; full_name: string; password: string; role: string; workspace_id: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const wsName = (id: string | null) => (id ? workspaces.find((w) => w.id === id)?.name ?? "workspace" : "organization");
  return (
    <Card actions={canManage && <Button variant="primary" size="sm" onClick={() => setForm({ email: "", full_name: "", password: "", role: "viewer", workspace_id: "" })}>Invite user</Button>} title="Users">
      {!users.data ? <Spinner /> : (
        <Table head={["User", "Roles", "Last login", "Status", ""]}>
          {users.data.items.map((u) => (
            <tr key={u.id}>
              <td className="px-4 py-2"><div className="font-medium">{u.full_name}</div><div className="text-xs text-muted">{u.email}</div></td>
              <td className="px-4 py-2"><div className="flex flex-wrap gap-1">{u.role_assignments.map((r) => <Badge key={r.id}>{r.role} · {wsName(r.workspace_id)}</Badge>)}</div></td>
              <td className="px-4 py-2 text-ink-2">{ago(u.last_login_at)}</td>
              <td className="px-4 py-2">{u.is_active ? <Badge>active</Badge> : <Badge className="text-critical">deactivated</Badge>}</td>
              <td className="px-4 py-2 text-right">
                {canManage && u.is_active && (
                  <Button size="sm" variant="ghost" onClick={async () => {
                    try {
                      await api(`/users/${u.id}`, { method: "PATCH", body: { is_active: false } });
                      users.reload();
                    } catch (e) { toast("error", (e as Error).message); }
                  }}>Deactivate</Button>
                )}
              </td>
            </tr>
          ))}
        </Table>
      )}
      {form && (
        <Modal open onClose={() => setForm(null)} title="Invite user" footer={<>
          <Button onClick={() => setForm(null)}>Cancel</Button>
          <Button variant="primary" onClick={async () => {
            try {
              await api("/users", { method: "POST", body: { ...form, workspace_id: form.workspace_id || null } });
              setForm(null);
              users.reload();
              toast("success", "User created");
            } catch (e) { setError((e as Error).message); }
          }}>Create</Button></>}>
          <div className="space-y-3">
            <ErrorNote message={error} />
            <Field label="Email"><Input value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} /></Field>
            <Field label="Full name"><Input value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} /></Field>
            <Field label="Initial password" hint="12+ chars, 3 of: lower, upper, digit, symbol"><Input type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Role"><Select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>{ROLES.map((r) => <option key={r}>{r}</option>)}</Select></Field>
              <Field label="Scope"><Select value={form.workspace_id} onChange={(e) => setForm({ ...form, workspace_id: e.target.value })}>
                <option value="">Entire organization</option>
                {workspaces.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
              </Select></Field>
            </div>
          </div>
        </Modal>
      )}
    </Card>
  );
}

function Teams({ canManage }: { canManage: boolean }) {
  const teams = useData<Team[]>("/teams");
  const [name, setName] = useState("");
  return (
    <Card title="Teams" actions={canManage && (
      <div className="flex gap-2">
        <Input className="h-8 w-48" placeholder="New team name" value={name} onChange={(e) => setName(e.target.value)} />
        <Button size="sm" disabled={!name} onClick={async () => { await api("/teams", { method: "POST", body: { name } }); setName(""); teams.reload(); }}>Add</Button>
      </div>)}>
      {!teams.data ? <Spinner /> : (
        <Table head={["Team", "Description", "Members"]} empty={teams.data.length === 0}>
          {teams.data.map((t) => <tr key={t.id}><td className="px-4 py-2 font-medium">{t.name}</td><td className="px-4 py-2 text-ink-2">{t.description}</td><td className="px-4 py-2 tabular">{t.member_ids.length}</td></tr>)}
        </Table>
      )}
    </Card>
  );
}

function Keys({ workspaces }: { workspaces: { id: string; name: string }[] }) {
  const keys = useData<ApiKey[]>("/api-keys");
  const [created, setCreated] = useState<string | null>(null);
  const [form, setForm] = useState({ name: "", role: "operator", workspace_id: "" });
  return (
    <Card title="API keys">
      <div className="flex flex-wrap items-end gap-2 border-b border-line p-3">
        <Field label="Name"><Input className="w-56" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
        <Field label="Role"><Select className="w-44" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>{ROLES.map((r) => <option key={r}>{r}</option>)}</Select></Field>
        <Field label="Scope"><Select className="w-52" value={form.workspace_id} onChange={(e) => setForm({ ...form, workspace_id: e.target.value })}>
          <option value="">Organization</option>{workspaces.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}</Select></Field>
        <Button variant="primary" disabled={!form.name} onClick={async () => {
          const r = await api<{ key: string }>("/api-keys", { method: "POST", body: { ...form, workspace_id: form.workspace_id || null } });
          setCreated(r.key);
          keys.reload();
        }}>Create key</Button>
      </div>
      {created && (
        <div className="m-3 rounded-md bg-warning/15 p-3 text-sm ring-1 ring-inset ring-warning/40">
          Copy this key now — it is shown only once: <code className="break-all font-mono">{created}</code>
        </div>
      )}
      {!keys.data ? <Spinner /> : (
        <Table head={["Name", "Prefix", "Role", "Last used", "Status", ""]} empty={keys.data.length === 0}>
          {keys.data.map((k) => (
            <tr key={k.id}>
              <td className="px-4 py-2 font-medium">{k.name}</td>
              <td className="px-4 py-2 font-mono text-xs">ffk_{k.prefix}_…</td>
              <td className="px-4 py-2">{k.role}</td>
              <td className="px-4 py-2 text-ink-2">{ago(k.last_used_at)}</td>
              <td className="px-4 py-2">{k.revoked_at ? <Badge className="text-critical">revoked</Badge> : <Badge>active</Badge>}</td>
              <td className="px-4 py-2 text-right">{!k.revoked_at && <Button size="sm" variant="ghost" onClick={async () => { await api(`/api-keys/${k.id}`, { method: "DELETE" }); keys.reload(); }}>Revoke</Button>}</td>
            </tr>
          ))}
        </Table>
      )}
    </Card>
  );
}
