import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { FB, expand, treeRow } from "./fixtures";

/**
 * Roadmap 2.4: capacity and transaction-ID forecasting. The forecast tab of a database shows its size, the
 * transaction counter, sequences against the columns they feed and table formats; an observation can be
 * recorded on demand and a space budget set. (backend/tests/test_forecast.py covers trends and alerts.)
 */
test.describe.configure({ mode: "serial" });

const SERVER = `E2E Forecast ${Date.now().toString(36)}`;
let page: Page;
let serverId = "";
let dbId = "";

test.beforeAll(async ({ browser, request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, {
    data: { name: "forecastdb", path: `${FB.dir}/fbadmin_e2efc_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  const base = `/api/servers/${serverId}/databases/${dbId}`;
  for (const sql of [
    "CREATE SEQUENCE SEQ_ORDERS",
    "CREATE TABLE ORDERS (ID INTEGER NOT NULL PRIMARY KEY, NOTE VARCHAR(20))",
    "CREATE TRIGGER ORDERS_BI FOR ORDERS BEFORE INSERT AS BEGIN IF (NEW.ID IS NULL) THEN NEW.ID = NEXT VALUE FOR SEQ_ORDERS; END",
    "ALTER SEQUENCE SEQ_ORDERS RESTART WITH 2000000000",
  ]) expect((await (await request.post(base + "/execute", { data: { sql } })).json()).error, sql).toBeNull();
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

test("the capacity forecast of a database", async () => {
  await expand(page, SERVER);
  await treeRow(page, "forecastdb").click({ button: "right" });
  const ctx = page.locator(".ctx").first();
  const target = page.locator(".ctx button[role=menuitem]", { hasText: "Capacity forecast…" }).first();
  if (!(await target.isVisible())) {
    for (const sub of await ctx.locator(":scope > .ctx-sub > button").all()) {
      await sub.click();
      if (await target.isVisible()) break;
    }
  }
  await target.click();
  await expect(pane().getByText(/1 observation\(s\) since/)).toBeVisible();
  await expect(pane().getByText(/trends need observations at least 6 hours apart/)).toBeVisible();

  // the sequence feeding an INTEGER key, 93 % used
  const row = pane().getByRole("region", { name: "Sequences" }).locator("tr", { hasText: "SEQ_ORDERS" });
  await expect(row).toContainText("ORDERS.ID");
  await expect(row).toContainText("INTEGER");
  await expect(row).toContainText("93.13 %");
  await expect(row).toContainText("warning");
  await expect(pane().getByText(/^Transactions$/)).toBeVisible();

  await pane().getByRole("button", { name: "Record an observation now" }).click();
  await expect(pane().getByText(/2 observation\(s\) since/)).toBeVisible();

  // a space budget, far above the size
  await pane().getByLabel("Space budget (GB)").fill("10");
  await pane().getByRole("button", { name: "Save", exact: true }).click();
  await expect(pane().getByText("Budget reached in")).toBeVisible();
  await expect(pane().locator(".fc-kv").first()).toContainText("10.0 GB");

  for (const theme of ["light", "dark", "contrast"]) {
    await page.evaluate((th) => { document.documentElement.dataset.theme = th; }, theme);
    await audit(`forecast (${theme} theme)`);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
});
