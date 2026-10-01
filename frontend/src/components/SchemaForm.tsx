"use client";

/**
 * Renders a configuration form from a node/connector JSON Schema (Pydantic-generated).
 * Every scalar field also accepts `{{ expression }}` templates, which the engine renders at run time.
 */

import { Plus, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import type { Connection, ConnectorInfo, JsonSchema } from "@/lib/types";
import { titleCase } from "@/lib/format";
import { Button, cn, inputClass, Select } from "./ui";

const MULTILINE = new Set(["prompt", "text", "body", "description", "system", "sql", "instructions", "goal", "policy", "html", "condition", "text_body", "user_prompt", "system_prompt"]);

function resolve(schema: JsonSchema, root: JsonSchema): JsonSchema {
  let s = schema;
  for (let i = 0; i < 10 && s.$ref; i++) {
    const name = s.$ref.split("/").pop()!;
    s = { ...(root.$defs?.[name] ?? {}), ...Object.fromEntries(Object.entries(s).filter(([k]) => k !== "$ref")) };
  }
  if (s.allOf?.length === 1) s = { ...resolve(s.allOf[0], root), ...Object.fromEntries(Object.entries(s).filter(([k]) => k !== "allOf")) };
  if (s.anyOf) {
    const nonNull = s.anyOf.filter((x) => x.type !== "null");
    if (nonNull.length === 1) return { ...resolve(nonNull[0], root), title: s.title, description: s.description, default: s.default };
    return { ...s, anyOf: nonNull };
  }
  return s;
}

function typeOf(s: JsonSchema): string {
  if (s["x-connection"]) return "connection";
  if (s.enum) return "enum";
  const t = Array.isArray(s.type) ? s.type.find((x) => x !== "null") : s.type;
  if (t === "object" && !s.properties) return "json";
  if (t === "array") {
    return s.items && (s.items.properties || s.items.$ref) ? "array-object" : "array";
  }
  return t ?? "json";
}

function connectorMatches(filter: string, c: ConnectorInfo): boolean {
  if (filter.startsWith("category:")) return c.category === filter.slice(9);
  if (filter.startsWith("capability:")) return c.actions.some((a) => a.capability === filter.slice(11));
  return c.key === filter;
}

export type FormContext = { connections: Connection[]; connectors: ConnectorInfo[] };

function JsonInput({ value, onChange, placeholder }: { value: unknown; onChange: (v: unknown) => void; placeholder?: string }) {
  const toText = (v: unknown) => (v === undefined || v === null ? "" : typeof v === "string" ? v : JSON.stringify(v, null, 2));
  const [text, setText] = useState(toText(value));
  const [invalid, setInvalid] = useState(false);
  useEffect(() => setText(toText(value)), [value]);
  return (
    <textarea
      className={cn(inputClass, "h-auto min-h-20 py-2 font-mono text-xs", invalid && "border-critical")}
      value={text}
      placeholder={placeholder ?? 'JSON value or {{ expression }}'}
      onChange={(e) => setText(e.target.value)}
      onBlur={() => {
        const t = text.trim();
        if (t === "") return onChange(undefined);
        if (t.startsWith("{{")) {
          setInvalid(false);
          return onChange(t);
        }
        try {
          onChange(JSON.parse(t));
          setInvalid(false);
        } catch {
          setInvalid(!/^[^{[]/.test(t));
          onChange(t);
        }
      }}
    />
  );
}

function FieldInput({ name, schema, root, value, onChange, ctx, required }: { name: string; schema: JsonSchema; root: JsonSchema; value: unknown; onChange: (v: unknown) => void; ctx: FormContext; required: boolean }) {
  const s = resolve(schema, root);
  const kind = typeOf(s);
  const isExpr = typeof value === "string" && value.includes("{{");

  if (kind === "connection") {
    const filter = typeof s["x-connection"] === "string" ? (s["x-connection"] as string) : "";
    const allowed = new Set(ctx.connectors.filter((c) => !filter || connectorMatches(filter, c)).map((c) => c.key));
    const options = ctx.connections.filter((c) => allowed.has(c.connector_key));
    return (
      <Select value={(value as string) ?? ""} onChange={(e) => onChange(e.target.value || undefined)} aria-label={name}>
        <option value="">{required ? "Select a connection…" : "Workspace default"}</option>
        {options.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name} ({c.connector_key})
          </option>
        ))}
      </Select>
    );
  }
  if (kind === "enum" && !isExpr) {
    return (
      <Select value={value === undefined || value === null ? "" : String(value)} onChange={(e) => onChange(e.target.value === "" ? undefined : e.target.value)} aria-label={name}>
        {!required && <option value="">—</option>}
        {s.enum!.map((o) => (
          <option key={String(o)} value={String(o)}>
            {String(o)}
          </option>
        ))}
      </Select>
    );
  }
  if (kind === "boolean" && !isExpr) {
    return (
      <label className="flex items-center gap-2 text-sm text-ink-2">
        <input type="checkbox" className="size-4 accent-[var(--accent)]" checked={Boolean(value ?? s.default)} onChange={(e) => onChange(e.target.checked)} />
        {s.description ?? "Enabled"}
      </label>
    );
  }
  if (kind === "integer" || kind === "number") {
    return (
      <input
        className={inputClass}
        aria-label={name}
        value={value === undefined || value === null ? "" : String(value)}
        placeholder={s.default !== undefined ? String(s.default) : "Number or {{ expression }}"}
        onChange={(e) => {
          const t = e.target.value;
          if (t === "") return onChange(undefined);
          const n = Number(t);
          onChange(Number.isFinite(n) && !t.includes("{{") ? n : t);
        }}
      />
    );
  }
  if (kind === "string") {
    const multiline = s["x-multiline"] || MULTILINE.has(name);
    const common = {
      "aria-label": name,
      value: value === undefined || value === null ? "" : String(value),
      placeholder: s.default !== undefined && s.default !== null ? String(s.default) : "Text or {{ expression }}",
      onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => onChange(e.target.value === "" ? undefined : e.target.value),
    };
    return multiline ? <textarea {...common} className={cn(inputClass, "h-auto min-h-20 py-2 font-mono text-xs")} /> : <input {...common} className={inputClass} />;
  }
  if (kind === "array" && !isExpr) {
    const arr = Array.isArray(value) ? value : [];
    return (
      <textarea
        aria-label={name}
        className={cn(inputClass, "h-auto min-h-16 py-2 font-mono text-xs")}
        placeholder="One value per line"
        value={arr.map(String).join("\n")}
        onChange={(e) => {
          const items = e.target.value.split("\n").map((x) => x.trim()).filter(Boolean);
          onChange(items.length ? items : undefined);
        }}
      />
    );
  }
  if (kind === "array-object" && !isExpr) {
    const items = Array.isArray(value) ? (value as Record<string, unknown>[]) : [];
    const itemSchema = resolve(s.items!, root);
    return (
      <div className="space-y-2">
        {items.map((item, i) => (
          <div key={i} className="relative rounded-md border border-line bg-surface-2 p-3">
            <button
              type="button"
              aria-label="Remove item"
              className="absolute right-2 top-2 rounded p-1 text-muted hover:text-critical"
              onClick={() => onChange(items.filter((_, j) => j !== i))}
            >
              <Trash2 className="size-3.5" />
            </button>
            <ObjectFields schema={itemSchema} root={root} value={item} onChange={(v) => onChange(items.map((x, j) => (j === i ? v : x)))} ctx={ctx} compact />
          </div>
        ))}
        <Button type="button" size="sm" icon={<Plus className="size-3.5" />} onClick={() => onChange([...items, {}])}>
          Add
        </Button>
      </div>
    );
  }
  if (kind === "object" && !isExpr) {
    return (
      <div className="rounded-md border border-line p-3">
        <ObjectFields schema={s} root={root} value={(value as Record<string, unknown>) ?? {}} onChange={onChange} ctx={ctx} compact />
      </div>
    );
  }
  return <JsonInput value={value} onChange={onChange} />;
}

export function ObjectFields({ schema, root, value, onChange, ctx, compact, hide = [] }: { schema: JsonSchema; root: JsonSchema; value: Record<string, unknown>; onChange: (v: Record<string, unknown>) => void; ctx: FormContext; compact?: boolean; hide?: string[] }) {
  const props = schema.properties ?? {};
  const required = new Set(schema.required ?? []);
  return (
    <div className={cn(compact ? "space-y-2.5" : "space-y-4")}>
      {Object.entries(props)
        .filter(([k]) => !hide.includes(k))
        .map(([key, propSchema]) => {
          const s = resolve(propSchema, root);
          const label = s.title && s.title !== titleCase(key) ? s.title : titleCase(key);
          const showDescription = typeOf(s) !== "boolean" && s.description;
          return (
            <div key={key}>
              <div className="mb-1 flex items-baseline justify-between gap-2">
                <span className="text-xs font-medium text-ink-2">
                  {label}
                  {required.has(key) && <span className="text-critical"> *</span>}
                </span>
                <code className="text-[10px] text-muted">{key}</code>
              </div>
              <FieldInput
                name={key}
                schema={propSchema}
                root={root}
                value={value?.[key]}
                required={required.has(key)}
                ctx={ctx}
                onChange={(v) => {
                  const next = { ...(value ?? {}) };
                  if (v === undefined) delete next[key];
                  else next[key] = v;
                  onChange(next);
                }}
              />
              {showDescription && <p className="mt-1 text-xs text-muted">{s.description}</p>}
            </div>
          );
        })}
    </div>
  );
}

export function SchemaForm({ schema, value, onChange, ctx, hide }: { schema: JsonSchema; value: Record<string, unknown>; onChange: (v: Record<string, unknown>) => void; ctx: FormContext; hide?: string[] }) {
  return <ObjectFields schema={resolve(schema, schema)} root={schema} value={value} onChange={onChange} ctx={ctx} hide={hide} />;
}
