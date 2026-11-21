import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { FB, expand, treeRow } from "./fixtures";

/**
 * Roadmap 2.5: unused index finder. With the history recorded and no plan using it, an index is a candidate
 * to drop; constraint indices are kept; the DROP statements can be proposed as a change request.
 * (backend/tests/test_unused_indices.py covers the plan formats and the statuses.)
 */
test.describe.configure({ mode: "serial" });

const SERVER = `E2E Unused ${Date.now().toString(36)}`;
let page: Page;
let serverId = "";
let dbId = "";
const base = () => `/api/servers/${serverId}/databases/${dbId}`;

test.beforeAll(async ({ browser, request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, {
    data: { name: "idxdb", path: `${FB.dir}/fbadmin_e2eui_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  for (const sql of ["CREATE TABLE ORD (ID INTEGER NOT NULL PRIMARY KEY, NOTE VARCHAR(20))", "CREATE INDEX IX_ORD_NOTE ON ORD (NOTE)"]) {
    expect((await (await request.post(base() + "/execute", { data: { sql } })).json()).error).toBeNull();
  }
  expect((await request.put(base() + "/monitoring/history/tracking", { data: { enabled: true } })).status()).toBe(200);
  page = await (await browser.newContext()).newPage();
  await page.goto("/");
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  await request.put(base() + "/monitoring/history/tracking", { data: { enabled: false } });
  await request.delete(base() + "/monitoring/history");
  await request.post(`${base()}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

test("candidates to drop, proposed as a change request", async () => {
  await expand(page, SERVER);
  await treeRow(page, "idxdb").click({ button: "right" });
  const ctx = page.locator(".ctx").first();
  const target = page.locator(".ctx button[role=menuitem]", { hasText: "Unused indices…" }).first();
  if (!(await target.isVisible())) {
    for (const sub of await ctx.locator(":scope > .ctx-sub > button").all()) {
      await sub.click();
      if (await target.isVisible()) break;
    }
  }
  await target.click();
  const pane = page.locator(".tab-pane:not(.hidden)");
  const grid = pane.getByRole("region", { name: "Indices" });
  await expect(grid).toContainText("IX_ORD_NOTE");
  await expect(grid).not.toContainText("enforces a constraint");
  await expect(pane).toContainText("Little evidence");
  await pane.getByLabel("Show every index").check();
  await expect(grid).toContainText("enforces a constraint");
  for (const theme of ["light", "dark", "contrast"]) {
    await page.evaluate((th) => { document.documentElement.dataset.theme = th; }, theme);
    const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
    expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), theme).toEqual([]);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
  await pane.getByRole("button", { name: "Propose as a change request…" }).click();
  await expect(page.locator(".tab-pane:not(.hidden) .cr-editor .cm-content")).toHaveText("DROP INDEX IX_ORD_NOTE;");
});
