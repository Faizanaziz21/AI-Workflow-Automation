"use client";

import { Area, AreaChart, Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { money } from "@/lib/format";

/* Chart tokens: categorical slots 1-2 (blue, orange) for identity; recessive grid/axes; text in ink tokens. */
const AXIS = { stroke: "var(--axis)", tick: { fill: "var(--ink-muted)", fontSize: 11 }, tickLine: false };

function timeLabel(iso: string, bucket: string) {
  const d = new Date(iso);
  if (bucket === "day") return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  if (bucket === "hour") return d.toLocaleTimeString(undefined, { hour: "2-digit" });
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

function ChartTooltip({ active, payload, label, format }: { active?: boolean; payload?: { name: string; value: number; color: string }[]; label?: string; format?: (v: number) => string }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-md border border-line bg-surface px-3 py-2 text-xs shadow-lg">
      <div className="mb-1 font-medium text-ink">{label}</div>
      {payload.map((p) => (
        <div key={p.name} className="flex items-center gap-2 text-ink-2">
          <span className="inline-block size-2 rounded-sm" style={{ background: p.color }} aria-hidden />
          <span className="flex-1">{p.name}</span>
          <span className="tabular font-medium text-ink">{format ? format(p.value) : p.value.toLocaleString()}</span>
        </div>
      ))}
    </div>
  );
}

export function ExecutionsChart({ data, bucket }: { data: { t: string; completed: number; failed: number }[]; bucket: string }) {
  const rows = data.map((d) => ({ label: timeLabel(d.t, bucket), Completed: d.completed, Failed: d.failed }));
  return (
    <figure aria-label="Executions over time, completed and failed">
      <ResponsiveContainer width="100%" height={240}>
        <BarChart data={rows} margin={{ top: 8, right: 8, left: -12, bottom: 0 }} barCategoryGap="20%">
          <CartesianGrid vertical={false} stroke="var(--grid)" />
          <XAxis dataKey="label" {...AXIS} minTickGap={24} />
          <YAxis {...AXIS} allowDecimals={false} axisLine={false} width={40} />
          <Tooltip content={<ChartTooltip />} cursor={{ fill: "var(--surface-2)" }} />
          <Legend iconType="square" iconSize={8} wrapperStyle={{ fontSize: 12, color: "var(--ink-2)" }} />
          <Bar dataKey="Completed" stackId="a" fill="var(--series-1)" stroke="var(--surface)" strokeWidth={1} maxBarSize={28} />
          <Bar dataKey="Failed" stackId="a" fill="var(--series-2)" stroke="var(--surface)" strokeWidth={1} radius={[4, 4, 0, 0]} maxBarSize={28} />
        </BarChart>
      </ResponsiveContainer>
    </figure>
  );
}

export function AICostChart({ data, bucket }: { data: { t: string; cost_usd: number; tokens: number }[]; bucket: string }) {
  const rows = data.map((d) => ({ label: timeLabel(d.t, bucket), "AI cost": d.cost_usd }));
  return (
    <figure aria-label="AI cost over time">
      <ResponsiveContainer width="100%" height={240}>
        <AreaChart data={rows} margin={{ top: 8, right: 8, left: -4, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke="var(--grid)" />
          <XAxis dataKey="label" {...AXIS} minTickGap={24} />
          <YAxis {...AXIS} axisLine={false} width={52} tickFormatter={(v: number) => money(v)} />
          <Tooltip content={<ChartTooltip format={money} />} cursor={{ stroke: "var(--axis)" }} />
          <Area type="monotone" dataKey="AI cost" stroke="var(--series-1)" strokeWidth={2} fill="var(--series-1)" fillOpacity={0.12} dot={false} activeDot={{ r: 4 }} />
        </AreaChart>
      </ResponsiveContainer>
    </figure>
  );
}

export function StatTile({ label, value, sub }: { label: string; value: string; sub?: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-line bg-surface p-4">
      <div className="text-xs font-medium text-ink-2">{label}</div>
      <div className="mt-1 text-2xl font-semibold tracking-tight text-ink">{value}</div>
      {sub && <div className="mt-1 text-xs text-muted">{sub}</div>}
    </div>
  );
}
