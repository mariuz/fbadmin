import AxeBuilder from "@axe-core/playwright";
import { expect, request as pwRequest, test, type APIRequestContext, type Page } from "@playwright/test";
import { spawn, type ChildProcess } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, join, resolve } from "node:path";
import { FB, expand, treeRow } from "./fixtures";

/**
 * Roadmap 2.3: AI inspection of the performance check-up. This spec starts its own fbadmin with a canned,
 * test-only AI provider (e2e/fake-ai/sitecustomize.py): the report is inspected, the answer shows a diagnosis,
 * ranked recommendations and SQL, a follow-up question is answered, and "What is sent" shows the prompt.
 */
test.describe.configure({ mode: "serial" });

const PORT = Number(process.env.E2E_AI_PORT ?? Number(process.env.E2E_PORT ?? 5106) + 1200);
const BASE = `http://127.0.0.1:${PORT}`;
const SERVER = `E2E Checkup AI ${Date.now().toString(36)}`;
let proc: ChildProcess | null = null;
let dataDir = "";
let api: APIRequestContext;
let page: Page;
let serverId = "";
let dbId = "";

async function run(sql: string) {
  const s = await (await api.post("/api/query/sessions", { data: { server_id: serverId, database_id: dbId } })).json();
  const r = await (await api.post(`/api/query/sessions/${s.session_id}/execute`, { data: { sql, autocommit: true } })).json();
  expect(r.error, JSON.stringify(r.error)).toBeNull();
  await api.delete(`/api/query/sessions/${s.session_id}`);
}

test.beforeAll(async ({ browser }) => {
  dataDir = mkdtempSync(join(tmpdir(), "fbadmin-e2e-ai-"));
  const fake = resolve(__dirname, "../fake-ai");
  proc = spawn(process.env.FBADMIN_PYTHON ?? "python3", ["-m", "fbadmin", "--no-browser", "--host", "127.0.0.1", "--port", String(PORT), "--log-level", "warning"], {
    cwd: resolve(__dirname, "../../backend"),
    env: { ...process.env, FBADMIN_DATA_DIR: dataDir, FBADMIN_AI_PROVIDER: "e2e-fake",
           PYTHONPATH: [fake, process.env.PYTHONPATH].filter(Boolean).join(delimiter) },
    stdio: "ignore",
  });
  api = await pwRequest.newContext({ baseURL: BASE });
  for (let i = 0; i < 100; i++) {
    try { if ((await api.get("/api/health")).ok()) break; } catch { /* starting */ }
    await new Promise((r) => setTimeout(r, 300));
  }
  expect((await (await api.get("/api/ai/status")).json()).enabled).toBe(true);
  serverId = (await (await api.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  const db = await api.post(`/api/servers/${serverId}/databases/create`, {
    data: { name: "aidb", path: `${FB.dir}/fbadmin_e2ecuai_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  await run("CREATE TABLE ORDERS (ID INTEGER NOT NULL PRIMARY KEY, CUSTOMER INTEGER);\nCREATE INDEX IX_CUSTOMER ON ORDERS (CUSTOMER);");
  await run("EXECUTE BLOCK AS DECLARE I INTEGER = 0; BEGIN WHILE (I < 500) DO BEGIN INSERT INTO ORDERS VALUES (:I, MOD(:I, 9)); I = I + 1; END END");
  page = await (await browser.newContext({ baseURL: BASE })).newPage();
  await page.goto(BASE + "/");
});

test.afterAll(async () => {
  await page?.context().close();
  if (serverId) {
    await api.post(`/api/servers/${serverId}/databases/${dbId}/drop`);
    await api.delete(`/api/servers/${serverId}`);
  }
  await api?.dispose();
  proc?.kill();
  if (dataDir) rmSync(dataDir, { recursive: true, force: true });
});

const pane = () => page.locator(".tab-pane:not(.hidden)");

async function audit(what: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
  expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), what).toEqual([]);
}

test("the AI inspects the check-up and answers a follow-up", async () => {
  await expand(page, SERVER);
  await treeRow(page, "aidb").click({ button: "right" });
  const ctx = page.locator(".ctx").first();
  const target = page.locator(".ctx button[role=menuitem]", { hasText: "Performance check-up…" }).first();
  if (!(await target.isVisible())) {
    for (const sub of await ctx.locator(":scope > .ctx-sub > button").all()) {
      await sub.click();
      if (await target.isVisible()) break;
    }
  }
  await target.click();
  await pane().getByRole("button", { name: "Run the check-up" }).click();
  await expect(pane().getByRole("list", { name: "Findings, most important first" })).toBeVisible({ timeout: 60_000 });

  await expect(pane()).toContainText("e2e-fake · canned-1");
  await pane().getByRole("button", { name: "What is sent…" }).click();
  const sent = pane().getByRole("region", { name: "What is sent" }).or(pane().getByLabel("What is sent"));
  await expect(sent).toContainText("Findings, most important first:");
  await expect(sent).toContainText("CREATE TABLE ORDERS");
  await pane().getByRole("button", { name: "What is sent…" }).click();

  await pane().getByRole("button", { name: "Inspect with AI" }).click();
  const answer = pane().locator(".cu-ai").first();
  await expect(answer.getByRole("heading", { name: "Diagnosis" })).toBeVisible({ timeout: 30_000 });
  await expect(answer.locator("ol li").first()).toContainText("Recompute selectivity of IX_CUSTOMER");
  await expect(answer.getByLabel("SQL")).toContainText("SET STATISTICS INDEX IX_CUSTOMER;");
  await expect(answer).toContainText("e2e-fake · canned-1");

  await pane().getByLabel("Ask a follow-up question").fill("Is the cache big enough?");
  await pane().getByRole("button", { name: "Ask", exact: true }).click();
  const second = pane().locator(".cu-ai").nth(1);
  await expect(second).toContainText("Question: Is the cache big enough?", { timeout: 30_000 });
  await expect(second.locator("code")).toHaveText("2048");
  for (const theme of ["light", "dark", "contrast"]) {
    await page.evaluate((th) => { document.documentElement.dataset.theme = th; }, theme);
    await audit(`AI inspection (${theme} theme)`);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });

  await answer.getByRole("button", { name: "Open in Query Tool" }).click();
  await expect(page.locator(".cm-content").last()).toContainText("SET STATISTICS INDEX IX_CUSTOMER;");
});
