"use client";

import { Search } from "lucide-react";
import { useMemo, useState } from "react";
import type { NodeTypeInfo } from "@/lib/types";
import { inputClass } from "../ui";

const CATEGORY_LABEL: Record<string, string> = {
  trigger: "Triggers",
  logic: "Logic",
  data: "Data",
  ai: "AI",
  communication: "Communication",
  business: "Business integrations",
  human: "Human",
};

export function NodePalette({ types, onAdd, hasTrigger }: { types: NodeTypeInfo[]; onAdd: (t: NodeTypeInfo) => void; hasTrigger: boolean }) {
  const [q, setQ] = useState("");
  const groups = useMemo(() => {
    const needle = q.toLowerCase();
    const filtered = types.filter((t) => !needle || t.label.toLowerCase().includes(needle) || t.type.includes(needle) || t.description.toLowerCase().includes(needle));
    const out = new Map<string, NodeTypeInfo[]>();
    for (const t of filtered) {
      if (!out.has(t.category)) out.set(t.category, []);
      out.get(t.category)!.push(t);
    }
    return [...out.entries()];
  }, [types, q]);

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-line p-3">
        <div className="relative">
          <Search className="absolute left-2.5 top-2.5 size-4 text-muted" aria-hidden />
          <input className={`${inputClass} pl-8`} placeholder="Search 75+ nodes…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search nodes" />
        </div>
      </div>
      <div className="flex-1 overflow-y-auto p-2">
        {groups.map(([cat, items]) => (
          <div key={cat} className="mb-3">
            <div className="px-2 pb-1 text-[11px] font-semibold uppercase tracking-wide text-muted">{CATEGORY_LABEL[cat] ?? cat}</div>
            {items.map((t) => {
              const disabled = t.is_trigger && hasTrigger;
              return (
                <button
                  key={t.type}
                  draggable={!disabled}
                  onDragStart={(e) => e.dataTransfer.setData("application/flowforge-node", t.type)}
                  disabled={disabled}
                  onClick={() => onAdd(t)}
                  title={disabled ? "A workflow has exactly one trigger" : t.description}
                  className="block w-full rounded-md px-2 py-1.5 text-left hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <div className="truncate text-sm text-ink">{t.label}</div>
                  <div className="truncate text-[11px] text-muted">{t.description}</div>
                </button>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}
