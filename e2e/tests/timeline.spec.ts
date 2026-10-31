import AxeBuilder from "@axe-core/playwright";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { FB, expand, treeRow } from "./fixtures";

/**
 * Roadmap 2.3: database timeline. Changes made through fbadmin (DDL from a dialog, a failing statement,
 * a sweep job, a property change) show up in one history of the database, newest first, grouped by day,
 * with category filters, search and details. (backend/tests/test_timeline.py covers the other sources.)
 */
test.describe.configure({ mode: "serial" });

const SERVER = `E2E Timeline ${Date.now().toString(36)}`;
let page: Page;
let serverId = "";
let dbId = "";

async function waitJob(request: APIRequestContext, id: string) {
  for (let i = 0; i < 200; i++) {
    const j = await (await request.get(`/api/jobs/${id}`)).json();
    if (j.status !== "running") return j;
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error("job did not end");
}

test.beforeAll(async ({ browser, request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, {
    data: { name: "historydb", path: `${FB.dir}/fbadmin_e2etl_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  const base = `/api/servers/${serverId}/databases/${dbId}`;
  expect((await (await request.post(base + "/execute", { data: { sql: "CREATE TABLE CUSTOMERS (ID INTEGER NOT NULL PRIMARY KEY, NAME VARCHAR(40))" } })).json()).error).toBeNull();
  await request.post(base + "/execute", { data: { sql: "CREATE TABLE CUSTOMERS (X INTEGER)" } });
  const job = await (await request.post(base + "/maintenance/sweep")).json();
  expect((await waitJob(request, job.id)).status).toBe("finished");
  expect((await request.put(base + "/maintenance/properties", { data: { sweep_interval: 15000 } })).status()).toBe(200);
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

test("one history of the database", async () => {
  await expand(page, SERVER);
  await treeRow(page, "historydb").click({ button: "right" });
  const ctx = page.locator(".ctx").first();
  const target = page.locator(".ctx button[role=menuitem]", { hasText: "Timeline…" }).first();
  if (!(await target.isVisible())) {
    for (const sub of await ctx.locator(":scope > .ctx-sub > button").all()) {
      await sub.click();
      if (await target.isVisible()) break;
    }
  }
  await target.click();
  const events = pane().locator(".tl-ev");
  await expect(events.first()).toBeVisible({ timeout: 15_000 });
  const titles = await pane().locator(".tl-title").allTextContents();
  expect(titles[0]).toContain("Properties changed");
  expect(titles).toContain("Sweep");
  expect(titles).toContain("DDL: CREATE TABLE CUSTOMERS (ID INTEGER NOT NULL PRIMARY KEY, NAME VARCHAR(40))");
  const failed = events.filter({ hasText: "DDL: CREATE TABLE CUSTOMERS (X INTEGER)" });
  await expect(failed.locator(".pill")).toHaveText("failed");
  await failed.getByRole("button", { name: "Details" }).click();
  await expect(failed.locator(".err")).toContainText("CUSTOMERS");
  await expect(pane().locator(".tl-day h4").first()).toBeVisible();

  for (const theme of ["light", "dark", "contrast"]) {
    await page.evaluate((th) => { document.documentElement.dataset.theme = th; }, theme);
    await audit(`timeline (${theme} theme)`);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });

  // category filter and search
  await pane().getByRole("button", { name: /^Maintenance \d+$/ }).click();
  await expect(pane().locator(".tl-title")).toHaveText([/Properties changed/, "Sweep"]);
  await pane().getByRole("button", { name: "Show all" }).click();
  await pane().getByLabel("Search the timeline").fill("customers");
  await expect(pane().locator(".tl-title")).toHaveCount(2);
  // what happened before a time in the past: nothing yet
  await pane().getByLabel("Search the timeline").fill("");
  await pane().locator("input[type=datetime-local]").fill("2001-01-01T00:00");
  await expect(pane()).toContainText("Nothing happened in this period.");
});
