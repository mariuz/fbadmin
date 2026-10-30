import AxeBuilder from "@axe-core/playwright";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { FB, expand, treeRow } from "./fixtures";

/**
 * Roadmap 2.2: query regression tracking. The tab turns history recording on, statements run in the Query
 * Tool are tracked with their plan, and a statement's runs, chart and plan are shown. (Regressions need an
 * older baseline: backend/tests/test_query_regressions.py covers them.)
 */
test.describe.configure({ mode: "serial" });

const SERVER = `E2E Regressions ${Date.now().toString(36)}`;
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
    data: { name: "shop", path: `${FB.dir}/fbadmin_e2ereg_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  await run(request, "CREATE TABLE ORDERS (ID INTEGER NOT NULL PRIMARY KEY, CUSTOMER INTEGER);\nINSERT INTO ORDERS VALUES (1, 10);");
  expect((await request.put("/api/monitoring/settings", { data: { regression_query_ms: 0 } })).status()).toBe(200);
  page = await (await browser.newContext()).newPage();
  await page.goto("/");
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  await request.put("/api/monitoring/settings", { data: { regression_query_ms: 200 } });
  await request.put(`/api/servers/${serverId}/databases/${dbId}/monitoring/history/tracking`, { data: { enabled: false } });
  await request.post(`/api/servers/${serverId}/databases/${dbId}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

const pane = () => page.locator(".tab-pane:not(.hidden)");

async function audit(what: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
  expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), what).toEqual([]);
}

test("tracked statements with their runs and plan", async ({ request }) => {
  await expand(page, SERVER);
  await treeRow(page, "shop").click({ button: "right" });
  const ctx = page.locator(".ctx").first();
  const target = page.locator(".ctx button[role=menuitem]", { hasText: "Query regressions…" }).first();
  if (!(await target.isVisible())) {
    for (const sub of await ctx.locator(":scope > .ctx-sub > button").all()) {
      await sub.click();
      if (await target.isVisible()) break;
    }
  }
  await target.click();
  await expect(pane()).toContainText("History isn't recorded for this database");
  await pane().getByRole("button", { name: "Record history for this database" }).click();
  await expect(pane()).not.toContainText("History isn't recorded for this database");

  for (const id of [1, 2, 3]) await run(request, `SELECT * FROM ORDERS WHERE ID = ${id}`);
  await pane().getByRole("button", { name: "All statements" }).click();
  const rows = pane().getByRole("region", { name: "Tracked statements" }).locator("tbody tr");
  await expect.poll(async () => (await rows.allTextContents()).join("\n"), { timeout: 15_000 }).toContain("select * from orders where id=?");
  const row = rows.filter({ hasText: "select * from orders where id=?" });
  await expect(row).toContainText("0 / 3");
  await row.click();
  const detail = pane().locator(".mt-section", { hasText: "Open in Query Tool" });
  await expect(detail.getByRole("img", { name: "Duration of each run over time, by plan" })).toBeVisible();
  await expect.poll(async () => (await detail.textContent()) ?? "", { timeout: 15_000 }).toContain("plan 1");
  await pane().getByRole("button", { name: "Regressions", exact: true }).click();
  await expect(pane()).toContainText("Nothing got worse.");
  await pane().getByRole("button", { name: "All statements" }).click();
  await pane().getByRole("button", { name: "Settings…" }).click();
  await expect(pane().getByLabel("Runs needed on each side")).toHaveValue("3");
  for (const theme of ["light", "dark", "contrast"]) {
    await page.evaluate((th) => { document.documentElement.dataset.theme = th; }, theme);
    await audit(`query regressions (${theme} theme)`);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
});
