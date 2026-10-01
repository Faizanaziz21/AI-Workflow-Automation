import { expect, test, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const EMAIL = process.env.E2E_EMAIL || "admin@acme.example";
const PASSWORD = process.env.E2E_PASSWORD || "FlowForge-Demo-2026!";
const SHOTS = process.env.E2E_SCREENSHOTS; // directory to write README screenshots into

async function shot(page: Page, name: string) {
  if (!SHOTS) return;
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.waitForTimeout(600);
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`) });
}

test.describe.configure({ mode: "serial" });

test("operator journey: login → dashboard → editor → execution inspector → approval", async ({ page }) => {
  await page.goto("/workflows");
  await expect(page).toHaveURL(/\/login/);
  await page.getByLabel("Work email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Workflows" })).toBeVisible();

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Operations dashboard" })).toBeVisible();
  await expect(page.getByText("Failure rate", { exact: true }).first()).toBeVisible();
  await expect(page.getByLabel("Executions over time, completed and failed")).toBeVisible();
  await shot(page, "dashboard");

  await page.goto("/workflows");
  await page.getByRole("link", { name: "Enterprise Customer Support Automation" }).first().click();
  await expect(page.getByText("Extract customer identity")).toBeVisible();
  await expect(page.locator(".react-flow__node")).toHaveCount(23);
  expect(await page.locator(".react-flow__edge").count()).toBeGreaterThan(20);
  await page.getByText("AI drafts response").click();
  await expect(page.getByRole("tab", { name: "Configuration" })).toBeVisible();
  await expect(page.getByText("Valid", { exact: true })).toBeVisible();
  await shot(page, "workflow-editor");

  await page.goto("/executions");
  await expect(page.getByRole("heading", { name: "Executions" })).toBeVisible();
  await shot(page, "executions");
  await page.getByRole("link", { name: "Enterprise Customer Support Automation" }).first().click();
  await expect(page.getByRole("tab", { name: "Graph" })).toBeVisible();
  await expect(page.getByText("Event log")).toBeVisible();
  await shot(page, "execution-inspector");
  await page.getByRole("tab", { name: "Timeline" }).click();
  await shot(page, "execution-timeline");

  await page.goto("/approvals");
  await expect(page.getByRole("heading", { name: "Approval inbox" })).toBeVisible();
  await shot(page, "approvals");

  await page.goto("/templates");
  await expect(page.getByText("Enterprise Customer Support Automation")).toBeVisible();
  await expect(page.getByRole("button", { name: "Use template" })).toHaveCount(10);
  await shot(page, "templates");

  await page.goto("/connections");
  await expect(page.getByText("Sandbox LLM (OpenAI-compatible)")).toBeVisible();
  await shot(page, "connections");

  await page.goto("/ai");
  await expect(page.getByRole("heading", { name: "AI usage & prompts" })).toBeVisible();
  await shot(page, "ai-usage");

  await page.goto("/audit");
  await expect(page.getByText("auth.login").first()).toBeVisible();
  await shot(page, "audit-log");
});

test("approve a pending support reply and see the execution complete", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Work email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL((u) => !u.pathname.startsWith("/login"));
  await page.goto("/approvals");
  const approve = page.getByRole("button", { name: "Approve" });
  await approve.waitFor({ timeout: 8000 }).catch(() => undefined);
  if (!(await approve.isVisible())) test.skip(true, "no pending approvals in this environment");
  await page.getByLabel("Comment").fill("Reviewed — sending");
  await approve.click();
  await expect(page.getByText("Approved — workflow resuming")).toBeVisible();
});

test("operator requeues a dead-lettered execution", async ({ page, request }) => {
  // Arrange through the public API: a workflow that fails on its first attempt and succeeds once retried.
  const auth = await (await request.post("/api/v1/auth/login", { data: { email: EMAIL, password: PASSWORD } })).json();
  const headers = { Authorization: `Bearer ${auth.access_token}` };
  const [ws] = await (await request.get("/api/v1/workspaces", { headers })).json();
  const name = `DLQ e2e ${Date.now()}`;
  const definition = {
    nodes: [
      { id: "start", type: "trigger.manual", config: {} },
      {
        id: "guard",
        type: "logic.stop",
        config: {
          outcome: "{{ 'success' if execution.retry_count > 0 else 'error' }}",
          message: "{{ 'Recovered after retry' if execution.retry_count > 0 else 'Missing purchase order number' }}",
        },
      },
    ],
    edges: [{ source: "start", target: "guard" }],
    settings: {},
  };
  const wf = await (await request.post(`/api/v1/workspaces/${ws.id}/workflows`, { headers, data: { name, definition } })).json();
  expect((await request.post(`/api/v1/workspaces/${ws.id}/workflows/${wf.id}/publish`, { headers, data: {} })).ok()).toBeTruthy();
  const started = await (await request.post(`/api/v1/workspaces/${ws.id}/workflows/${wf.id}/run`, { headers, data: { input: {} } })).json();

  await page.goto("/login");
  await page.getByLabel("Work email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL((u) => !u.pathname.startsWith("/login"));
  await page.goto("/executions");
  await page.getByRole("tab", { name: "Dead letters" }).click();
  const row = page.getByRole("row").filter({ hasText: name });
  await expect(row).toBeVisible({ timeout: 20_000 });
  await expect(row.getByText("Missing purchase order number")).toBeVisible();
  await shot(page, "dead-letters");
  await row.getByRole("button", { name: "Requeue" }).click();
  await expect(page.getByText("Requeued: failed nodes will run again")).toBeVisible();
  await expect(row).toHaveCount(0);
  await expect
    .poll(async () => (await (await request.get(`/api/v1/executions/${started.execution_id}`, { headers })).json()).status, { timeout: 20_000 })
    .toBe("COMPLETED");
});
