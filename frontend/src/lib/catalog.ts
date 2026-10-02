"use client";

import { useEffect, useState } from "react";
import { api } from "./api";
import { useData } from "./auth";
import type { Connection, ConnectorInfo, NodeTypeInfo } from "./types";

export function useCatalog(workspaceId: string | undefined) {
  const nodeTypes = useData<NodeTypeInfo[]>("/node-types");
  const connectors = useData<ConnectorInfo[]>("/connectors");
  const connections = useData<Connection[]>(workspaceId ? `/workspaces/${workspaceId}/connections` : null);
  return {
    nodeTypes: nodeTypes.data ?? [],
    connectors: connectors.data ?? [],
    connections: connections.data ?? [],
    ready: !!nodeTypes.data && !!connectors.data,
    reloadConnections: connections.reload,
  };
}

/** Output handles per node from the backend (dynamic handles depend on config). */
export function useHandles(nodes: { id: string; type: string; config: Record<string, unknown>; on_error?: string }[] | undefined) {
  const [handles, setHandles] = useState<Record<string, string[]>>({});
  const key = JSON.stringify(nodes?.map((n) => [n.id, n.type, n.config, n.on_error]) ?? []);
  useEffect(() => {
    if (!nodes?.length) return;
    const t = setTimeout(() => {
      api<Record<string, string[]>>("/node-types/handles", {
        method: "POST",
        body: { nodes: nodes.map((n) => ({ id: n.id, type: n.type, config: n.config, on_error: n.on_error })) },
      })
        .then(setHandles)
        .catch(() => undefined);
    }, 250);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return handles;
}
