"use client";

import { useState } from "react";
import { StatTile } from "@/components/charts";
import { Badge, Button, Card, Drawer, ErrorNote, Field, Input, JsonView, PageHeader, Select, Spinner, Table, Textarea, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { useAuth, useData } from "@/lib/auth";
import { compact, duration, money } from "@/lib/format";

type Usage = {
  totals: { calls: number; errors: number; input_tokens: number; output_tokens: number; cost_usd: number; fallbacks: number };
  groups: { key: string; calls: number; errors: number; input_tokens: number; output_tokens: number; cost_usd: number; avg_latency_ms: number; fallbacks: number }[];
};
type PromptRow = { key: string; description: string; builtin: boolean; latest_version: number; custom_versions: number };
type PromptVersion = { key: string; version: number; description: string; system_prompt: string; user_prompt: string; output_schema: unknown; is_active: boolean; builtin: boolean; created_at: string | null };

export default function AIPage() {
  const { can } = useAuth();
  const [days, setDays] = useState(30);
  const [groupBy, setGroupBy] = useState("model");
  const usage = useData<Usage>("/ai/usage", { days, group_by: groupBy });
  const prompts = useData<PromptRow[]>("/ai/prompts");
  const [openPrompt, setOpenPrompt] = useState<string | null>(null);
  const t = usage.data?.totals;

  return (
    <div className="mx-auto max-w-7xl">
      <PageHeader
        title="AI usage & prompts"
        subtitle="Every model call is metered: tokens, cost, latency, fallbacks and the exact prompt version used."
        actions={
          <>
            <Select aria-label="Period" className="w-36" value={days} onChange={(e) => setDays(Number(e.target.value))}>
              <option value={1}>Last 24 hours</option>
              <option value={7}>Last 7 days</option>
              <option value={30}>Last 30 days</option>
              <option value={90}>Last 90 days</option>
            </Select>
            <Select aria-label="Group by" className="w-36" value={groupBy} onChange={(e) => setGroupBy(e.target.value)}>
              {["model", "provider", "workflow", "prompt", "day"].map((g) => <option key={g} value={g}>By {g}</option>)}
            </Select>
          </>
        }
      />
      {t && (
        <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-5">
          <StatTile label="Calls" value={compact(t.calls)} sub={`${t.errors} errors`} />
          <StatTile label="Cost" value={money(t.cost_usd)} />
          <StatTile label="Input tokens" value={compact(t.input_tokens)} />
          <StatTile label="Output tokens" value={compact(t.output_tokens)} />
          <StatTile label="Fallbacks used" value={compact(t.fallbacks)} />
        </div>
      )}
      <Card title={`Usage by ${groupBy}`}>
        {!usage.data ? <Spinner /> : (
          <Table head={[groupBy, "Calls", "Errors", "Input tok.", "Output tok.", "Cost", "Avg latency", "Fallbacks"]} empty={usage.data.groups.length === 0}>
            {usage.data.groups.map((g) => (
              <tr key={g.key}>
                <td className="px-4 py-2 font-mono text-xs">{g.key}</td>
                <td className="px-4 py-2 tabular">{g.calls}</td>
                <td className="px-4 py-2 tabular">{g.errors}</td>
                <td className="px-4 py-2 tabular">{compact(g.input_tokens)}</td>
                <td className="px-4 py-2 tabular">{compact(g.output_tokens)}</td>
                <td className="px-4 py-2 tabular">{money(g.cost_usd)}</td>
                <td className="px-4 py-2 tabular">{duration(g.avg_latency_ms)}</td>
                <td className="px-4 py-2 tabular">{g.fallbacks}</td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
      <Card title="Prompt library" className="mt-4">
        {!prompts.data ? <Spinner /> : (
          <Table head={["Key", "Description", "Active version", ""]}>
            {prompts.data.map((p) => (
              <tr key={p.key}>
                <td className="px-4 py-2 font-mono text-xs">{p.key}</td>
                <td className="px-4 py-2 text-ink-2">{p.description}</td>
                <td className="px-4 py-2">{p.latest_version ? <Badge>v{p.latest_version} (custom)</Badge> : <Badge>v0 (built-in)</Badge>}</td>
                <td className="px-4 py-2 text-right"><Button size="sm" onClick={() => setOpenPrompt(p.key)}>Versions</Button></td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
      {openPrompt && <PromptDrawer promptKey={openPrompt} canEdit={can("ai_prompts:manage")} onClose={() => { setOpenPrompt(null); prompts.reload(); }} />}
    </div>
  );
}

function PromptDrawer({ promptKey, canEdit, onClose }: { promptKey: string; canEdit: boolean; onClose: () => void }) {
  const versions = useData<PromptVersion[]>(`/ai/prompts/${promptKey}/versions`);
  const toast = useToast();
  const latest = versions.data?.[0];
  const [system, setSystem] = useState<string | null>(null);
  const [user, setUser] = useState<string | null>(null);
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);
  return (
    <Drawer open onClose={onClose} title={`Prompt: ${promptKey}`}>
      {!versions.data ? <Spinner /> : (
        <div className="space-y-4">
          {canEdit && latest && (
            <div className="space-y-2 rounded-md border border-line p-3">
              <div className="text-xs font-semibold">Publish a new version</div>
              <ErrorNote message={error} />
              <Field label="Description"><Input value={description} onChange={(e) => setDescription(e.target.value)} /></Field>
              <Field label="System prompt"><Textarea rows={4} value={system ?? latest.system_prompt} onChange={(e) => setSystem(e.target.value)} /></Field>
              <Field label="User prompt template" hint="Inputs are available as {{ input.* }}">
                <Textarea rows={6} value={user ?? latest.user_prompt} onChange={(e) => setUser(e.target.value)} />
              </Field>
              <Button
                variant="primary"
                size="sm"
                onClick={async () => {
                  try {
                    await api(`/ai/prompts/${promptKey}/versions`, {
                      method: "POST",
                      body: { description, system_prompt: system ?? latest.system_prompt, user_prompt: user ?? latest.user_prompt, output_schema: latest.output_schema },
                    });
                    toast("success", "New prompt version published; unpinned nodes use it immediately");
                    versions.reload();
                  } catch (e) {
                    setError((e as Error).message);
                  }
                }}
              >
                Publish version
              </Button>
            </div>
          )}
          {versions.data.map((v) => (
            <div key={v.version} className="rounded-md border border-line p-3">
              <div className="flex items-center gap-2 text-sm font-semibold">
                v{v.version} {v.builtin && <Badge>built-in</Badge>} {!v.is_active && <Badge>inactive</Badge>}
              </div>
              {v.description && <p className="text-xs text-ink-2">{v.description}</p>}
              <div className="mt-2 text-xs font-medium text-ink-2">System</div>
              <pre className="whitespace-pre-wrap rounded bg-surface-2 p-2 text-xs">{v.system_prompt}</pre>
              <div className="mt-2 text-xs font-medium text-ink-2">User</div>
              <pre className="whitespace-pre-wrap rounded bg-surface-2 p-2 text-xs">{v.user_prompt}</pre>
              {v.output_schema ? <details className="mt-2 text-xs"><summary className="cursor-pointer text-ink-2">Output schema</summary><JsonView value={v.output_schema} /></details> : null}
            </div>
          ))}
        </div>
      )}
    </Drawer>
  );
}
