import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { FB, expand, treeRow, shot } from "./fixtures";

/**
 * Roadmap 2.5: maintenance windows. A recurring window for a database is created from its menu and listed with
 * its next opening; it can be paused and deleted. (backend/tests/test_windows.py covers muted alerts, the scope
 * and change requests applied in a window.)
 */
test.describe.configure({ mode: "serial" });

const SERVER = `E2E Windows ${Date.now().toString(36)}`;
let page: Page;
let serverId = "";

test.beforeAll(async ({ browser, request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  expect((await request.post(`/api/servers/${serverId}/databases`, { data: { name: "windb", path: "/nowhere/windb.fdb" } })).status()).toBe(201);
  page = await (await browser.newContext()).newPage();
  await page.goto("/");
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  for (const w of await (await request.get("/api/maintenance-windows")).json()) {
    if (w.server_id === serverId) await request.delete(`/api/maintenance-windows/${w.id}`);
  }
  await request.delete(`/api/servers/${serverId}`);
});

test("a recurring window for a database", async ({ request }) => {
  await expand(page, SERVER);
  await treeRow(page, "windb").click({ button: "right" });
  const ctx = page.locator(".ctx").first();
  const target = page.locator(".ctx button[role=menuitem]", { hasText: "Maintenance windows…" }).first();
  if (!(await target.isVisible())) {
    for (const sub of await ctx.locator(":scope > .ctx-sub > button").all()) {
      await sub.click();
      if (await target.isVisible()) break;
    }
  }
  await target.click();
  const pane = page.locator(".tab-pane:not(.hidden)");
  await expect(pane).toContainText("No maintenance window covers this database.");
  await pane.getByRole("button", { name: "New window" }).click();
  await pane.getByLabel("Name").fill("Sunday night");
  await pane.getByLabel("Opens (cron)").fill("0 23 * * 0");
  await pane.getByLabel("Lasts (minutes)").fill("90");
  for (const theme of ["light", "dark", "contrast"]) {
    await page.evaluate((th) => { document.documentElement.dataset.theme = th; }, theme);
    const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
    expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), theme).toEqual([]);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
  await pane.getByRole("button", { name: "Save", exact: true }).click();
  const grid = pane.getByRole("region", { name: "Maintenance windows" });
  await expect(grid).toContainText("Sunday night");
  await expect(grid).toContainText("0 23 * * 0, 90 min");
  await shot(page, "maintenance-windows"); // docs/screenshots/maintenance-windows.png
  const ws = (await (await request.get("/api/maintenance-windows")).json()).filter((w: { server_id: string }) => w.server_id === serverId);
  expect(ws.length).toBe(1);
  expect(new Date(ws[0].next_start * 1000).getDay()).toBe(0);
  await grid.getByRole("button", { name: "Pause" }).click();
  await expect(grid).toContainText("⏸ Sunday night");
});
