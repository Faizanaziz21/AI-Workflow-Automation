export type Me = {
  user: { id: string; email: string; full_name: string } | null;
  organization: { id: string; name: string; slug: string };
  actor_type: string;
  roles: string[];
  workspace_roles: Record<string, string[]>;
  permissions: string[];
  team_ids: string[];
};

export type Workspace = { id: string; name: string; slug: string; description: string };

export type JsonSchema = {
  type?: string | string[];
  title?: string;
  description?: string;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  items?: JsonSchema;
  enum?: unknown[];
  default?: unknown;
  anyOf?: JsonSchema[];
  allOf?: JsonSchema[];
  $ref?: string;
  $defs?: Record<string, JsonSchema>;
  additionalProperties?: boolean | JsonSchema;
  minimum?: number;
  maximum?: number;
  format?: string;
  const?: unknown;
  "x-connection"?: string | boolean;
  "x-multiline"?: boolean;
};

export type NodeTypeInfo = {
  type: string;
  category: string;
  label: string;
  description: string;
  icon: string;
  config_schema: JsonSchema;
  output_schema: JsonSchema | null;
  handles: string[];
  dynamic_handles: boolean;
  is_trigger: boolean;
  connector_key: string | null;
  capability: string | null;
  raw_fields: string[];
};

export type NodeDef = {
  id: string;
  type: string;
  name: string;
  config: Record<string, unknown>;
  position: { x: number; y: number };
  retry?: { max_attempts: number; initial_interval_seconds: number; backoff_coefficient: number; max_interval_seconds?: number } | null;
  timeout_seconds?: number | null;
  on_error?: "fail" | "continue" | "route";
  disabled?: boolean;
  notes?: string;
};

export type EdgeDef = { id?: string | null; source: string; target: string; source_handle: string; target_handle?: string };

export type Definition = {
  schema_version?: number;
  nodes: NodeDef[];
  edges: EdgeDef[];
  settings: Record<string, unknown>;
  variables: Record<string, unknown>;
};

export type Version = {
  id: string;
  version: number;
  status: string;
  revision: number;
  definition_hash: string;
  change_note: string;
  created_at: string;
  published_at: string | null;
  definition: Definition;
};

export type Workflow = {
  id: string;
  workspace_id: string;
  name: string;
  description: string;
  tags: string[];
  status: string;
  latest_version: number;
  template_slug: string | null;
  updated_at: string;
  trigger_type: string | null;
  published_version: number | null;
  has_draft: boolean;
  executions_24h?: number | null;
  draft?: Version | null;
  published?: Version | null;
};

export type ExecutionSummary = {
  id: string;
  workflow_id: string;
  workflow_name: string | null;
  workflow_version: number | null;
  status: string;
  trigger_type: string;
  correlation_key: string | null;
  created_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  retry_count: number;
  is_paused: boolean;
  error: { message?: string; node_id?: string; code?: string } | null;
};

export type NodeRun = {
  id: string;
  node_id: string;
  node_type: string;
  scope: string;
  status: string;
  attempt: number;
  max_attempts: number;
  branches: string[] | null;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  wait_until: string | null;
  error: { message?: string; code?: string; details?: unknown } | null;
  input?: unknown;
  output?: unknown;
  idempotency_key: string;
};

export type ExecutionDetail = ExecutionSummary & {
  trigger_payload: unknown;
  output: unknown;
  definition: { nodes: NodeDef[]; edges: EdgeDef[] };
  node_runs: NodeRun[];
  sensitive_data_visible: boolean;
};

export type ExecEvent = {
  id: number;
  type: string;
  level: string;
  message: string;
  node_id: string | null;
  scope: string | null;
  data: Record<string, unknown>;
  created_at: string;
};

export type Approval = {
  id: string;
  execution_id: string;
  node_id: string;
  kind: string;
  title: string;
  description: string;
  context: Record<string, unknown>;
  form_schema: JsonSchema | null;
  status: string;
  due_at: string | null;
  escalation_level: number;
  approvals_count: number;
  required_approvals: number;
  response_data: Record<string, unknown> | null;
  created_at: string;
  workflow_name: string | null;
  can_decide: boolean;
  decision_comment: string | null;
  history?: { action: string; actor_email: string | null; comment: string | null; created_at: string }[];
};

export type Connection = {
  id: string;
  name: string;
  connector_key: string;
  auth_type: string;
  config: Record<string, unknown>;
  status: string;
  status_message: string | null;
  last_tested_at: string | null;
  credential_fields: string[];
  secret_version: number | null;
  can_use: boolean;
  created_at: string;
};

export type ConnectorInfo = {
  key: string;
  name: string;
  description: string;
  category: string;
  icon: string;
  auth: { type: string; description: string; config_schema: JsonSchema; credentials_schema: JsonSchema; oauth2: unknown };
  actions: { key: string; name: string; capability: string | null }[];
  triggers: { key: string; name: string }[];
};

export type Template = {
  slug: string;
  name: string;
  category: string;
  summary: string;
  description: string;
  tags: string[];
  connection_roles: Record<string, string>;
  featured: boolean;
  install_count: number;
  node_count: number;
  node_types: string[];
};
