import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { FB, expand, treeRow } from "./fixtures";

/**
 * Roadmap 2.4: scheduled performance check-up. From the check-up page, Schedule… creates a "checkup" schedule;
 * Run now runs it, the page shows the scheduled run's report and the Schedules tab its result.
 * (backend/tests/test_checkup_schedule.py covers the alerts: new findings only, resolved, the selectivity fix.)
 */
test.describe.configure({ mode: "serial" });

const SERVER = `E2E Checkup schedule ${Date.now().toString(36)}`;
let page: Page;
let serverId = "";
let dbId = "";

test.beforeAll(async ({ browser, request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, {
    data: { name: "nightlydb", path: `${FB.dir}/fbadmin_e2ecs_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  const base = `/api/servers/${serverId}/databases/${dbId}`;
  for (const sql of [
    "CREATE TABLE LOADED (ID INTEGER NOT NULL, CODE VARCHAR(10))",
    "CREATE INDEX IX_LOADED_CODE ON LOADED (CODE)",
    "EXECUTE BLOCK AS DECLARE I INTEGER = 0; BEGIN WHILE (I < 500) DO BEGIN INSERT INTO LOADED VALUES (:I, MOD(:I, 7)); I = I + 1; END END",
  ]) expect((await (await request.post(base + "/execute", { data: { sql } })).json()).error, sql).toBeNull();
  page = await (await browser.newContext()).newPage();
  await page.goto("/");
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  for (const s of await (await request.get("/api/schedules")).json()) {
    if (s.database_id === dbId) await request.delete(`/api/schedules/${s.id}`);
  }
  await request.post(`/api/servers/${serverId}/databases/${dbId}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

const pane = () => page.locator(".tab-pane:not(.hidden)");

async function audit(what: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
  expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), what).toEqual([]);
}

test("schedule the check-up and run it", async () => {
  test.setTimeout(150_000);
  await expand(page, SERVER);
  await treeRow(page, "nightlydb").click({ button: "right" });
  const ctx = page.locator(".ctx").first();
  const target = page.locator(".ctx button[role=menuitem]", { hasText: "Performance check-up…" }).first();
  if (!(await target.isVisible())) {
    for (const sub of await ctx.locator(":scope > .ctx-sub > button").all()) {
      await sub.click();
      if (await target.isVisible()) break;
    }
  }
  await target.click();
  await pane().getByRole("button", { name: "Schedule…" }).click();
  const dlg = page.getByRole("dialog");
  await expect(dlg.getByLabel("Name")).toHaveValue("Daily check-up nightlydb");
  await expect(dlg.getByText(/^Next: /)).toBeVisible();
  await dlg.getByLabel("Include gstat (reads the whole database)").uncheck();
  await dlg.getByLabel("Alert on").selectOption("warning");
  await dlg.getByLabel("Page cache too small").check();
  for (const theme of ["light", "dark", "contrast"]) {
    await page.evaluate((th) => { document.documentElement.dataset.theme = th; }, theme);
    await audit(`schedule check-up dialog (${theme} theme)`);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
  await dlg.getByRole("button", { name: "Schedule it" }).click();
  const row = dlg.getByRole("region", { name: "Scheduled check-ups of this database" }).locator("tr", { hasText: "Daily check-up nightlydb" });
  await expect(row).toContainText("0 6 * * *");
  await row.getByRole("button", { name: "Run now" }).click();
  await expect(page.getByText("Run finished")).toBeVisible({ timeout: 60_000 });
  await expect(row).toContainText("findings");
  await page.keyboard.press("Escape");
  await expect(dlg).toBeHidden();

  // the page shows the scheduled run's report
  await expect(pane().getByText(/by the scheduled check-up Daily check-up nightlydb/)).toBeVisible();
  await expect(pane().getByRole("list", { name: "Findings, most important first" })).toContainText("have no selectivity");
});

test("the schedule and its history", async ({ request }) => {
  const s = (await (await request.get("/api/schedules")).json()).find((x: { database_id: string }) => x.database_id === dbId);
  expect(s.kind).toBe("checkup");
  expect(s.checkup).toEqual({ gstat: false, min_severity: "warning", fix_selectivity: false, ignore: ["page_cache"] });
  const h = (await (await request.get(`/api/schedules/${s.id}/history`)).json())[0];
  expect(h.status).toBe("findings");
  expect(h.new).toContain("selectivity_never");
});
