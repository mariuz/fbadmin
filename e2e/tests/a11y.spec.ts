import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { expand, FB, SCHEMA, setEditor, treeRow } from "./fixtures";

/**
 * Accessibility audit with axe-core (WCAG 2.1 A/AA rules) of the main screens, in the light,
 * dark and high-contrast themes, plus keyboard checks for the tree, menus, tabs and dialogs.
 */
test.describe.configure({ mode: "serial" });

const SERVER = "E2E A11y";
let page: Page;
let serverId = "";
let dbId = "";

test.beforeAll(async ({ browser, request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, {
    data: { name: "a11y", path: `${FB.dir}/fbadmin_e2ea_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  await request.post(`/api/servers/${serverId}/databases/${dbId}/execute`, { data: { sql: SCHEMA } });
  page = await (await browser.newContext()).newPage(); // AxeBuilder needs a page from an explicit context
  await page.goto("/");
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  if (!serverId) return;
  await request.post(`/api/servers/${serverId}/databases/${dbId}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

async function audit(what: string) {
  const r = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    // CodeMirror's editor internals are covered by its own accessibility work
    .exclude(".cm-editor")
    .analyze();
  const summary = r.violations.map((v) => `${v.id} (${v.impact}): ${v.nodes.length}× ${v.nodes.slice(0, 3).map((n) => n.target.join(" ")).join(" | ")}`);
  expect(summary, `${what}: axe violations`).toEqual([]);
}

test("main window, tree and properties have no axe violations", async () => {
  await expand(page, SERVER);
  await expand(page, "a11y");
  await expand(page, "Tables");
  await treeRow(page, "CUSTOMER").click();
  await expect(page.locator(".tab.active")).toHaveText("Properties");
  await audit("properties");
  for (const theme of ["dark", "contrast"]) {
    await page.evaluate((th) => { document.documentElement.dataset.theme = th; }, theme);
    await audit(`properties (${theme} theme)`);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
});

test("Query Tool with results has no axe violations", async () => {
  await treeRow(page, "a11y").click();
  await page.locator(".topbar button", { hasText: "Query Tool" }).click();
  const qt = page.locator(".tab-pane:not(.hidden) .qt");
  await setEditor(page, qt, "SELECT * FROM CUSTOMER");
  await qt.locator("button", { hasText: "Execute" }).click();
  await expect(qt.locator(".qt-out table")).toContainText("Bucharest");
  await audit("query tool");
});

test("dialogs: labelled, focus moves in and returns, Escape closes", async () => {
  const opener = page.locator(".topbar button", { hasText: "Add server" });
  await opener.focus();
  await opener.press("Enter");
  const dlg = page.getByRole("dialog", { name: "Register server" });
  await expect(dlg).toBeVisible();
  await expect.poll(() => page.evaluate(() => !!document.activeElement?.closest(".modal"))).toBe(true);
  await audit("server dialog");
  // Tab stays inside the dialog
  for (let i = 0; i < 25; i++) await page.keyboard.press("Tab");
  expect(await page.evaluate(() => !!document.activeElement?.closest(".modal"))).toBe(true);
  await page.keyboard.press("Escape");
  await expect(dlg).toBeHidden();
  await expect(opener).toBeFocused();
});

test("context menu works from the keyboard", async () => {
  const row = treeRow(page, "CUSTOMER");
  await row.click();
  await row.press("Shift+F10");
  const menu = page.getByRole("menu").first();
  await expect(menu).toBeVisible();
  await expect(menu.getByRole("menuitem").first()).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(menu.getByRole("menuitem").nth(1)).toBeFocused();
  await audit("context menu");
  await page.keyboard.press("Escape");
  await expect(menu).toBeHidden();
});

test("work tabs are a keyboard-operable tablist", async () => {
  const tabs = page.getByRole("tablist", { name: "Panels" });
  await expect(tabs).toBeVisible();
  const selected = tabs.getByRole("tab", { selected: true });
  await selected.focus();
  const before = await selected.textContent();
  await page.keyboard.press("ArrowRight");
  const now = tabs.getByRole("tab", { selected: true });
  await expect(now).not.toHaveText(before ?? "");
  await expect(now).toBeFocused();
});
