"use client";

import { Handle, Position, type NodeProps } from "@xyflow/react";
import {
  Bot,
  Box,
  Clock,
  Database,
  GitBranch,
  Globe,
  Hand,
  Mail,
  MessageSquare,
  Play,
  Repeat,
  Users,
  Webhook,
  Zap,
} from "lucide-react";
import { memo } from "react";
import { StatusBadge, cn } from "../ui";
import type { NodeDef, NodeTypeInfo } from "@/lib/types";

export type FlowNodeData = {
  def: NodeDef;
  info?: NodeTypeInfo;
  handles: string[];
  issues: number;
  status?: string;
  durationMs?: number | null;
  attempts?: number;
};

const CATEGORY_STYLE: Record<string, { bar: string; Icon: typeof Box }> = {
  trigger: { bar: "bg-[var(--series-1)]", Icon: Zap },
  logic: { bar: "bg-[#4a3aa7] dark:bg-[#9085e9]", Icon: GitBranch },
  data: { bar: "bg-[#1baf7a]", Icon: Database },
  ai: { bar: "bg-[#e87ba4]", Icon: Bot },
  communication: { bar: "bg-[var(--series-2)]", Icon: MessageSquare },
  business: { bar: "bg-[#008300]", Icon: Globe },
  human: { bar: "bg-[#eda100]", Icon: Users },
};

function iconFor(type: string, category: string) {
  if (type === "trigger.webhook") return Webhook;
  if (type === "trigger.schedule" || type === "logic.delay" || type === "logic.wait_until") return Clock;
  if (type === "trigger.manual") return Play;
  if (type === "logic.loop") return Repeat;
  if (type.includes("email")) return Mail;
  if (type.startsWith("human.")) return Hand;
  return CATEGORY_STYLE[category]?.Icon ?? Box;
}

function FlowNodeComponent({ data, selected }: NodeProps) {
  const d = data as unknown as FlowNodeData;
  const category = d.info?.category ?? "business";
  const style = CATEGORY_STYLE[category] ?? CATEGORY_STYLE.business;
  const Icon = iconFor(d.def.type, category);
  const isTrigger = d.def.type.startsWith("trigger.");
  const handles = d.handles.length ? d.handles : [];
  return (
    <div
      className={cn(
        "relative w-56 rounded-lg border bg-surface shadow-sm transition-shadow",
        selected ? "border-accent ring-2 ring-accent/30" : "border-line",
        d.def.disabled && "opacity-50",
      )}
    >
      <div className={cn("absolute inset-y-0 left-0 w-1 rounded-l-lg", style.bar)} aria-hidden />
      {!isTrigger && <Handle type="target" position={Position.Left} id="in" />}
      <div className="flex items-start gap-2 py-2.5 pl-3.5 pr-3">
        <div className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-md bg-surface-2 text-ink-2">
          <Icon className="size-4" aria-hidden />
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium text-ink">{d.def.name || d.def.id}</div>
          <div className="truncate text-[11px] text-muted">{d.info?.label ?? d.def.type}</div>
        </div>
        {d.issues > 0 && (
          <span className="rounded-full bg-critical px-1.5 text-[10px] font-semibold text-white" title={`${d.issues} validation issue(s)`}>
            {d.issues}
          </span>
        )}
      </div>
      {d.status && (
        <div className="flex items-center justify-between border-t border-line px-3 py-1.5">
          <StatusBadge status={d.status} />
          <span className="text-[11px] text-muted tabular">
            {d.attempts && d.attempts > 1 ? `×${d.attempts} ` : ""}
            {d.durationMs != null ? `${d.durationMs < 1000 ? d.durationMs + " ms" : (d.durationMs / 1000).toFixed(1) + " s"}` : ""}
          </span>
        </div>
      )}
      {handles.map((h, i) => {
        const top = handles.length === 1 ? 50 : 22 + (i * 56) / Math.max(1, handles.length - 1);
        return (
          <div key={h}>
            <Handle type="source" position={Position.Right} id={h} style={{ top: `${top}%` }} />
            {handles.length > 1 && (
              <span className="pointer-events-none absolute left-full ml-2 -translate-y-1/2 whitespace-nowrap rounded bg-page px-1 text-[9px] uppercase tracking-wide text-ink-2" style={{ top: `${top}%` }}>
                {h}
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}

export const FlowNode = memo(FlowNodeComponent);
export const nodeTypes = { ff: FlowNode };
