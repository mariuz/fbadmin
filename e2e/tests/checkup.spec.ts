import AxeBuilder from "@axe-core/playwright";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { FB, expand, treeRow } from "./fixtures";

/**
 * Roadmap 2.3: guided performance check-up. Indices created on an empty table and then loaded have no
 * selectivity: the check-up ranks that finding, its fix recomputes it, and the check-up runs again.
 * (backend/tests/test_checkup.py covers the other findings: OAT, sweep, cache, locks, gstat, history.)
 */
test.describe.configure({ mode: "serial" });

const SERVER = `E2E Checkup ${Date.now().toString(36)}`;
let page: Page;
let serverId = "";
let dbId = "";

async function run(request: APIRequestContext, sql: string) {
  const s = await (await request.post("/api/query/sessions", { data: { server_id: serverId, database_id: dbId } })).json();
  const r = await (await request.post(`/api/query/sessions/${s.session_id}/execute`, { data: { sql, autocommit: true } })).json();
  expect(r.error, JSON.stringify(r.error)).toBeNull();
  await request.delete(`/api/query/sessions/${s.session_id}`);
}

test.beforeAll(async ({ browser, request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, {
    data: { name: "slowdb", path: `${FB.dir}/fbadmin_e2ecu_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  await run(request, "CREATE TABLE ORDERS (ID INTEGER NOT NULL PRIMARY KEY, CUSTOMER INTEGER);\nCREATE INDEX IX_CUSTOMER ON ORDERS (CUSTOMER);");
  await run(request, "EXECUTE BLOCK AS DECLARE I INTEGER = 0; BEGIN WHILE (I < 2000) DO BEGIN INSERT INTO ORDERS VALUES (:I, MOD(:I, 40)); I = I + 1; END END");
  page = await (await browser.newContext()).newPage();
  await page.goto("/");
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  await request.post(`/api/servers/${serverId}/databases/${dbId}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

const pane = () => page.locator(".tab-pane:not(.hidden)");

async function audit(what: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
  expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), what).toEqual([]);
}

test("a ranked report with a fix per finding", async () => {
  await expand(page, SERVER);
  await treeRow(page, "slowdb").click({ button: "right" });
  const ctx = page.locator(".ctx").first();
  const target = page.locator(".ctx button[role=menuitem]", { hasText: "Performance check-up…" }).first();
  if (!(await target.isVisible())) {
    for (const sub of await ctx.locator(":scope > .ctx-sub > button").all()) {
      await sub.click();
      if (await target.isVisible()) break;
    }
  }
  await target.click();
  await expect(pane()).toContainText("No check-up has run for this database yet.");
  await audit("check-up (empty)");
  await pane().getByRole("button", { name: "Run the check-up" }).click();

  const findings = pane().getByRole("list", { name: "Findings, most important first" });
  const never = findings.locator("li.cu-item", { hasText: "have no selectivity although their table has rows" });
  await expect(never).toBeVisible({ timeout: 60_000 });
  await expect(never).toContainText("IX_CUSTOMER");
  await expect(findings.locator("li.cu-item").first()).toContainText("1.");
  await expect(findings).toContainText("History isn't recorded for this database");
  await expect(pane()).toContainText("Database size");
  for (const theme of ["light", "dark", "contrast"]) {
    await page.evaluate((th) => { document.documentElement.dataset.theme = th; }, theme);
    await audit(`check-up (${theme} theme)`);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });

  await never.getByRole("button", { name: /Recompute selectivity of 2 index/ }).click();
  await expect(page.locator(".toast").last()).toContainText("Selectivity recomputed", { timeout: 30_000 });
  await expect(findings).not.toContainText("have no selectivity", { timeout: 60_000 });
  await expect(findings).toContainText("History isn't recorded for this database");
});
