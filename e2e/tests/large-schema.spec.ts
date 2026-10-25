import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { expand, FB, treeRow } from "./fixtures";

/**
 * Milestone 2.1 — large schemas: a folder with hundreds of objects renders only the rows in view
 * (virtual list), keeps its position numbers for screen readers, scrolls to any object, keeps
 * expanded tables expanded while scrolling, and Home/End reach the real first and last rows.
 */
test.describe.configure({ mode: "serial" });

const SERVER = "E2E Large schema";
const DB = "large";
const N = 600;
let page: Page;
let serverId = "";
let dbId = "";

test.beforeAll(async ({ browser, request }) => {
  test.setTimeout(240_000);
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  const path = `${FB.dir}/fbadmin_e2e_large_${Date.now().toString(36)}.fdb`;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, { data: { name: DB, path } });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  const ddl = Array.from({ length: N }, (_, i) =>
    `CREATE TABLE T${String(i).padStart(5, "0")} (ID INTEGER NOT NULL PRIMARY KEY, NAME VARCHAR(40));`).join("\n");
  const r = await (await request.post(`/api/servers/${serverId}/databases/${dbId}/execute`, { data: { sql: ddl }, timeout: 200_000 })).json();
  expect(r.error, JSON.stringify(r.error)).toBeNull();
  page = await (await browser.newContext()).newPage();
  await page.goto("/");
  await expand(page, SERVER);
  await expand(page, DB);
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  if (!serverId) return;
  await request.post(`/api/servers/${serverId}/databases/${dbId}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

test("a folder with hundreds of tables renders only the rows in view", async () => {
  await expand(page, "Tables");
  const first = treeRow(page, "T00000");
  await expect(first).toBeVisible();
  await expect(first).toHaveAttribute("aria-posinset", "1");
  await expect(first).toHaveAttribute("aria-setsize", String(N));
  const rows = await page.locator(".tree-row").count();
  expect(rows).toBeLessThan(200); // not 600+
  // expand a table: its columns appear and stay after scrolling away and back
  await expand(page, "T00002");
  await expect(treeRow(page, "NAME").first()).toBeVisible();
  const tree = page.locator(".tree");
  await tree.evaluate((el) => { el.scrollTop = el.scrollHeight; });
  await expect(treeRow(page, `T${String(N - 1).padStart(5, "0")}`)).toBeVisible();
  await expect(treeRow(page, "T00000")).toHaveCount(0); // unmounted while out of view
  await tree.evaluate((el) => { el.scrollTop = 0; });
  await expect(treeRow(page, "T00002")).toHaveAttribute("aria-expanded", "true");
  const r = await new AxeBuilder({ page }).include(".tree").withTags(["wcag2a", "wcag2aa"]).analyze();
  expect(r.violations.map((v) => v.id)).toEqual([]);
});

test("keyboard: End and Home reach the last and first rows", async () => {
  await treeRow(page, "T00001").click();
  await page.keyboard.press("End");
  await expect.poll(() => page.evaluate(() => document.activeElement?.closest(".tree-row")?.querySelector(".label")?.textContent))
    .not.toBe("T00001");
  const last = await page.evaluate(() => document.activeElement?.closest(".tree-row")?.querySelector(".label")?.textContent);
  const lastLabel = await page.locator(".tree-row .label").last().textContent();
  expect(last).toBe(lastLabel);
  await page.keyboard.press("Home");
  await expect.poll(() => page.evaluate(() => document.activeElement?.closest(".tree-row")?.querySelector(".label")?.textContent))
    .toBe(await page.locator(".tree-row .label").first().textContent());
});
