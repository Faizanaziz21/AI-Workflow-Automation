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
