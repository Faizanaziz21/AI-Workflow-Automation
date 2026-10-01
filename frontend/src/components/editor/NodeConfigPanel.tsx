"use client";

import { Trash2 } from "lucide-react";
import { useState } from "react";
import type { NodeDef, NodeTypeInfo } from "@/lib/types";
import { SchemaForm, type FormContext } from "../SchemaForm";
import { Field, Input, JsonView, Select, Tabs, Textarea } from "../ui";

type Issue = { level: string; message: string; node_id?: string; field?: string };

export function NodeConfigPanel({
  node,
  info,
  ctx,
  issues,
  onChange,
  onDelete,
  readOnly,
}: {
  node: NodeDef;
  info?: NodeTypeInfo;
  ctx: FormContext;
  issues: Issue[];
  onChange: (n: NodeDef) => void;
  onDelete: () => void;
  readOnly?: boolean;
}) {
  const [tab, setTab] = useState<"config" | "settings" | "output">("config");
  const retry = node.retry ?? null;
  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-line p-3">
        <Input aria-label="Node name" value={node.name} onChange={(e) => onChange({ ...node, name: e.target.value })} disabled={readOnly} className="font-medium" />
        <div className="mt-1.5 flex items-center justify-between text-[11px] text-muted">
          <span>
            {info?.label ?? node.type} · <code>{node.id}</code>
          </span>
          {!readOnly && (
            <button className="flex items-center gap-1 text-critical hover:underline" onClick={onDelete}>
              <Trash2 className="size-3" /> Delete
            </button>
          )}
        </div>
        {info?.description && <p className="mt-2 text-xs text-ink-2">{info.description}</p>}
      </div>
      {issues.length > 0 && (
        <ul className="space-y-1 border-b border-line bg-critical/5 p-3 text-xs text-critical">
          {issues.map((i, k) => (
            <li key={k}>
              {i.field ? <code>{i.field}: </code> : null}
              {i.message}
            </li>
          ))}
        </ul>
      )}
      <div className="px-3 pt-2">
        <Tabs
          value={tab}
          onChange={setTab}
          tabs={[
            { id: "config", label: "Configuration" },
            { id: "settings", label: "Reliability" },
            { id: "output", label: "Output schema" },
          ]}
        />
      </div>
      <fieldset disabled={readOnly} className="flex-1 overflow-y-auto p-3">
        {tab === "config" &&
          (info ? (
            <SchemaForm schema={info.config_schema} value={node.config} onChange={(config) => onChange({ ...node, config })} ctx={ctx} />
          ) : (
            <p className="text-sm text-critical">Unknown node type {node.type}</p>
          ))}
        {tab === "settings" && (
          <div className="space-y-3">
            <Field label="On error" hint="fail: stop the execution · continue: emit the error as output · route: follow the 'error' handle">
              <Select value={node.on_error ?? "fail"} onChange={(e) => onChange({ ...node, on_error: e.target.value as NodeDef["on_error"] })}>
                <option value="fail">Fail execution</option>
                <option value="continue">Continue with error output</option>
                <option value="route">Route to error branch</option>
              </Select>
            </Field>
            <Field label="Timeout (seconds)" hint="Empty = workflow/node default">
              <Input
                type="number"
                min={1}
                value={node.timeout_seconds ?? ""}
                onChange={(e) => onChange({ ...node, timeout_seconds: e.target.value ? Number(e.target.value) : null })}
              />
            </Field>
            <div className="rounded-md border border-line p-3">
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={!!retry}
                  onChange={(e) =>
                    onChange({ ...node, retry: e.target.checked ? { max_attempts: 3, initial_interval_seconds: 2, backoff_coefficient: 2, max_interval_seconds: 300 } : null })
                  }
                />
                Custom retry policy (exponential backoff with jitter)
              </label>
              {retry && (
                <div className="mt-3 grid grid-cols-2 gap-2">
                  <Field label="Max attempts">
                    <Input type="number" min={1} max={25} value={retry.max_attempts} onChange={(e) => onChange({ ...node, retry: { ...retry, max_attempts: Number(e.target.value) } })} />
                  </Field>
                  <Field label="Initial interval (s)">
                    <Input type="number" min={0} value={retry.initial_interval_seconds} onChange={(e) => onChange({ ...node, retry: { ...retry, initial_interval_seconds: Number(e.target.value) } })} />
                  </Field>
                  <Field label="Backoff coefficient">
                    <Input type="number" min={1} step={0.5} value={retry.backoff_coefficient} onChange={(e) => onChange({ ...node, retry: { ...retry, backoff_coefficient: Number(e.target.value) } })} />
                  </Field>
                  <Field label="Max interval (s)">
                    <Input type="number" min={0} value={retry.max_interval_seconds ?? 300} onChange={(e) => onChange({ ...node, retry: { ...retry, max_interval_seconds: Number(e.target.value) } })} />
                  </Field>
                </div>
              )}
            </div>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={!!node.disabled} onChange={(e) => onChange({ ...node, disabled: e.target.checked })} />
              Disabled (skipped during execution)
            </label>
            <Field label="Notes">
              <Textarea value={node.notes ?? ""} onChange={(e) => onChange({ ...node, notes: e.target.value })} />
            </Field>
          </div>
        )}
        {tab === "output" && (
          <div className="space-y-2 text-xs text-ink-2">
            <p>
              Reference this node&apos;s output downstream as <code className="text-ink">{`{{ nodes.${node.id}.output }}`}</code>.
            </p>
            <JsonView value={info?.output_schema ?? { description: "Free-form output" }} />
          </div>
        )}
      </fieldset>
      {readOnly && <div className="border-t border-line p-2 text-center text-xs text-muted">Read-only</div>}
    </div>
  );
}
