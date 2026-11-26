import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { FB, shot } from "./fixtures";

/**
 * Roadmap 2.5: fleet health. Every database in one table from fbadmin's records: a database with its history
 * recorded and an open change request, a fresh one with nothing recorded; filter, search, open a view.
 * (backend/tests/test_fleet.py covers each signal and the permissions.)
 */
test.describe.configure({ mode: "serial" });

const SERVER = `E2E Fleet ${Date.now().toString(36)}`;
let page: Page;
let serverId = "";
const dbIds: string[] = [];

test.beforeAll(async ({ browser, request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  for (const name of ["fleet_a", "fleet_b"]) {
    const db = await request.post(`/api/servers/${serverId}/databases/create`, {
      data: { name, path: `${FB.dir}/fbadmin_e2e${name}_${Date.now().toString(36)}.fdb` },
    });
    expect(db.status(), await db.text()).toBe(201);
    dbIds.push((await db.json()).id);
  }
  // fleet_a: history recorded and one open change request; fleet_b: nothing recorded
  expect((await request.put(`/api/servers/${serverId}/databases/${dbIds[0]}/monitoring/history/tracking`, { data: { enabled: true } })).status()).toBe(200);
  expect((await request.post("/api/changes", { data: {
    server_id: serverId, database_id: dbIds[0], title: "Add T1", sql: "CREATE TABLE T1 (X INTEGER)",
  } })).status()).toBe(201);
  page = await (await browser.newContext()).newPage();
  await page.goto("/");
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  await request.put(`/api/servers/${serverId}/databases/${dbIds[0]}/monitoring/history/tracking`, { data: { enabled: false } });
  for (const id of dbIds) await request.post(`/api/servers/${serverId}/databases/${id}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

test("every database in one table", async () => {
  await page.locator(".topbar").getByRole("button", { name: "Overview" }).click();
  await page.locator(".tab-pane:not(.hidden)").getByRole("button", { name: "Fleet health" }).click();
  const table = page.getByRole("region", { name: "Fleet health" });
  await page.getByLabel("Search").fill("fleet_");
  const rows = table.locator("tbody tr");
  await expect(rows).toHaveCount(2);
  const b = rows.filter({ hasText: "fleet_b" });
  await expect(b).toContainText("Unknown");
  await expect(b).toContainText("never run");
  await expect(b).toContainText("not scheduled");
  const a = rows.filter({ hasText: "fleet_a" });
  await expect(a).toContainText("OK");
  await expect(a).toContainText("1 open, 0 approved");
  await expect(a).toContainText(/sampled .* ago/);
  for (const theme of ["light", "dark", "contrast"]) {
    await page.evaluate((th) => { document.documentElement.dataset.theme = th; }, theme);
    const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
    expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), theme).toEqual([]);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
  await shot(page, "fleet-health"); // docs/screenshots/fleet-health.png
  await page.getByLabel("Show").selectOption("attention");
  await expect(page.getByText("No database matches.")).toBeVisible();
  await page.getByLabel("Show").selectOption("");
  await b.getByRole("button", { name: "never run" }).click();
  await expect(page.locator(".tab-pane:not(.hidden)")).toContainText("No check-up has run for this database yet.");
  await page.getByRole("tab", { name: "Fleet health" }).click();
  await a.getByRole("button", { name: /1 open/ }).click();
  await expect(page.locator(".tab-pane:not(.hidden)").getByRole("list", { name: "Change requests" })).toContainText("Add T1");
});
