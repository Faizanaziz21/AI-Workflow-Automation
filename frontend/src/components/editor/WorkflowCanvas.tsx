"use client";

import {
  applyNodeChanges,
  Background,
  BackgroundVariant,
  Controls,
  MarkerType,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Connection as RFConnection,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeChange,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { Definition, EdgeDef, NodeDef, NodeTypeInfo } from "@/lib/types";
import { nodeTypes, type FlowNodeData } from "./FlowNode";

export type NodeOverlay = Record<string, { status: string; durationMs?: number | null; attempts?: number }>;

type Props = {
  definition: Definition;
  typeIndex: Record<string, NodeTypeInfo>;
  handles: Record<string, string[]>;
  issuesByNode?: Record<string, number>;
  overlay?: NodeOverlay;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onChange?: (d: Definition) => void;
  onDropType?: (type: string, position: { x: number; y: number }) => void;
  readOnly?: boolean;
};

function edgeId(e: EdgeDef) {
  return e.id ?? `${e.source}:${e.source_handle}->${e.target}`;
}

function CanvasInner({ definition, typeIndex, handles, issuesByNode, overlay, selectedId, onSelect, onChange, onDropType, readOnly }: Props) {
  const rf = useReactFlow();

  const built: Node[] = useMemo(
    () =>
      definition.nodes.map((n) => ({
        id: n.id,
        type: "ff",
        position: n.position ?? { x: 0, y: 0 },
        selected: n.id === selectedId,
        data: {
          def: n,
          info: typeIndex[n.type],
          handles: handles[n.id] ?? typeIndex[n.type]?.handles ?? ["out"],
          issues: issuesByNode?.[n.id] ?? 0,
          status: overlay?.[n.id]?.status,
          durationMs: overlay?.[n.id]?.durationMs,
          attempts: overlay?.[n.id]?.attempts,
        } satisfies FlowNodeData as unknown as Record<string, unknown>,
      })),
    [definition.nodes, typeIndex, handles, issuesByNode, overlay, selectedId],
  );

  const edges: Edge[] = useMemo(
    () =>
      definition.edges.map((e) => {
        const status = overlay?.[e.target]?.status;
        const skipped = status === "SKIPPED";
        return {
          id: edgeId(e),
          source: e.source,
          target: e.target,
          sourceHandle: e.source_handle,
          targetHandle: "in",
          label: e.source_handle !== "out" ? e.source_handle : undefined,
          labelStyle: { fontSize: 10, fill: "var(--ink-2)" },
          labelBgStyle: { fill: "var(--surface)" },
          markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: "var(--axis)" },
          animated: status === "RUNNING",
          style: skipped ? { strokeDasharray: "4 4", opacity: 0.45 } : undefined,
        };
      }),
    [definition.edges, overlay],
  );

  // React Flow keeps measured dimensions in its own node state; the definition stays the source of truth.
  const [nodes, setNodes] = useState<Node[]>(built);
  useEffect(() => {
    setNodes((prev) => {
      const prevById = new Map(prev.map((n) => [n.id, n]));
      return built.map((n) => {
        const p = prevById.get(n.id);
        return p ? { ...p, ...n, measured: p.measured } : n;
      });
    });
  }, [built]);

  const onNodesChange = useCallback(
    (changes: NodeChange[]) => {
      const removed = new Set(changes.filter((c) => c.type === "remove").map((c) => (c as { id: string }).id));
      const local = changes.filter((c) => c.type !== "remove" && c.type !== "select" && (!readOnly || c.type === "dimensions"));
      if (local.length) setNodes((nds) => applyNodeChanges(local, nds));
      if (removed.size && !readOnly && onChange) {
        onChange({
          ...definition,
          nodes: definition.nodes.filter((n) => !removed.has(n.id)),
          edges: definition.edges.filter((e) => !removed.has(e.source) && !removed.has(e.target)),
        });
      }
    },
    [definition, onChange, readOnly],
  );

  const onNodeDragStop = useCallback(
    (_: unknown, __: Node, dragged: Node[]) => {
      if (readOnly || !onChange) return;
      const moved = new Map(dragged.map((n) => [n.id, n.position]));
      onChange({
        ...definition,
        nodes: definition.nodes.map((n) => {
          const p = moved.get(n.id);
          return p ? ({ ...n, position: { x: Math.round(p.x), y: Math.round(p.y) } } as NodeDef) : n;
        }),
      });
    },
    [definition, onChange, readOnly],
  );

  const onEdgesChange = useCallback(
    (changes: EdgeChange[]) => {
      if (readOnly || !onChange) return;
      const removed = new Set(changes.filter((c) => c.type === "remove").map((c) => c.id));
      if (removed.size) onChange({ ...definition, edges: definition.edges.filter((e) => !removed.has(edgeId(e))) });
    },
    [definition, onChange, readOnly],
  );

  const onConnect = useCallback(
    (c: RFConnection) => {
      if (readOnly || !onChange || !c.source || !c.target || c.source === c.target) return;
      const handle = c.sourceHandle ?? "out";
      if (definition.edges.some((e) => e.source === c.source && e.target === c.target && e.source_handle === handle)) return;
      onChange({ ...definition, edges: [...definition.edges, { source: c.source, target: c.target, source_handle: handle }] });
    },
    [definition, onChange, readOnly],
  );

  return (
    <div
      className="h-full w-full"
      onDragOver={(e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
      }}
      onDrop={(e) => {
        const type = e.dataTransfer.getData("application/flowforge-node");
        if (!type || !onDropType) return;
        e.preventDefault();
        onDropType(type, rf.screenToFlowPosition({ x: e.clientX, y: e.clientY }));
      }}
    >
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        onNodeDragStop={onNodeDragStop}
        onNodeClick={(_, n) => onSelect(n.id)}
        onPaneClick={() => onSelect(null)}
        nodesDraggable={!readOnly}
        nodesConnectable={!readOnly}
        elementsSelectable
        deleteKeyCode={readOnly ? null : ["Backspace", "Delete"]}
        fitView
        fitViewOptions={{ padding: 0.15, maxZoom: 1, minZoom: 0.55 }}
        minZoom={0.2}
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={18} size={1} color="var(--axis)" />
        <Controls showInteractive={false} />
        <MiniMap pannable zoomable nodeStrokeWidth={3} maskColor="rgba(0,0,0,0.06)" />
      </ReactFlow>
    </div>
  );
}

export function WorkflowCanvas(props: Props) {
  return (
    <ReactFlowProvider>
      <CanvasInner {...props} />
    </ReactFlowProvider>
  );
}
