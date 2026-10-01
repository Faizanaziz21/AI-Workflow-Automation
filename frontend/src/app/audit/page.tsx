"use client";

import { Search } from "lucide-react";
import { Fragment, useState } from "react";
import { Card, Input, JsonView, PageHeader, Select, Spinner, StatusBadge, Table } from "@/components/ui";
import { useData } from "@/lib/auth";
import { dateTime } from "@/lib/format";

type Entry = {
  id: number;
  actor_type: string;
  actor_email: string | null;
  action: string;
  resource_type: string | null;
  resource_id: string | null;
  summary: string;
  outcome: string;
  ip_address: string | null;
  request_id: string | null;
  details: Record<string, unknown>;
  created_at: string;
};

const ACTIONS = ["", "auth.*", "workflow.*", "execution.*", "approval.*", "connection.*", "user.*", "api_key.*", "template.*", "secrets.*"];

export default function AuditPage() {
  const [q, setQ] = useState("");
  const [action, setAction] = useState("");
  const [outcome, setOutcome] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const logs = useData<{ items: Entry[]; total: number }>("/audit-logs", { q: q || undefined, action: action || undefined, outcome: outcome || undefined, limit: 200 });
  return (
    <div className="mx-auto max-w-7xl">
      <PageHeader title="Audit log" subtitle="Immutable record of security-relevant and administrative activity. Values of secrets are never recorded." />
      <Card>
        <div className="flex flex-wrap gap-2 border-b border-line p-3">
          <div className="relative w-80">
            <Search className="absolute left-2.5 top-2.5 size-4 text-muted" aria-hidden />
            <Input className="pl-8" placeholder="Full-text search (actor, action, summary)" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search audit log" />
          </div>
          <Select className="w-48" value={action} onChange={(e) => setAction(e.target.value)} aria-label="Action">
            {ACTIONS.map((a) => <option key={a} value={a}>{a || "All actions"}</option>)}
          </Select>
          <Select className="w-36" value={outcome} onChange={(e) => setOutcome(e.target.value)} aria-label="Outcome">
            <option value="">Any outcome</option>
            <option value="success">Success</option>
            <option value="failure">Failure</option>
            <option value="denied">Denied</option>
          </Select>
          {logs.data && <span className="self-center text-xs text-muted">{logs.data.total.toLocaleString()} events</span>}
        </div>
        {!logs.data ? <Spinner /> : (
          <Table head={["Time", "Actor", "Action", "Summary", "Outcome", "IP"]} empty={logs.data.items.length === 0}>
            {logs.data.items.map((e) => (
              <Fragment key={e.id}>
                <tr className="cursor-pointer hover:bg-surface-2" onClick={() => setOpen(open === e.id ? null : e.id)}>
                  <td className="whitespace-nowrap px-4 py-2 text-xs text-ink-2">{dateTime(e.created_at)}</td>
                  <td className="px-4 py-2 text-xs">{e.actor_email ?? e.actor_type}</td>
                  <td className="px-4 py-2 font-mono text-xs">{e.action}</td>
                  <td className="max-w-md truncate px-4 py-2 text-sm">{e.summary}</td>
                  <td className="px-4 py-2"><StatusBadge status={e.outcome} /></td>
                  <td className="px-4 py-2 text-xs text-muted">{e.ip_address ?? "—"}</td>
                </tr>
                {open === e.id && (
                  <tr>
                    <td colSpan={6} className="bg-surface-2 px-4 py-3">
                      <JsonView value={{ resource: `${e.resource_type}:${e.resource_id}`, request_id: e.request_id, details: e.details }} />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </Table>
        )}
      </Card>
    </div>
  );
}
