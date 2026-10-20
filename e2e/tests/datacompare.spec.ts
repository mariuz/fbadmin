import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { expand, FB, treeRow } from "./fixtures";

/**
 * Milestone 1.2 — data compare: open the tab from a database's context menu, compare two
 * databases, look at the differing rows, download the sync script, apply it to the target
 * (with the typed confirmation) and compare again with no differences left.
 */
test.describe.configure({ mode: "serial" });

const SERVER = "E2E Data compare";
const SRC = "dc_src";
const TGT = "dc_tgt";
let page: Page;
let serverId = "";
const dbIds: string[] = [];

const DDL = `
CREATE TABLE CATEGORY (ID INTEGER NOT NULL PRIMARY KEY, NAME VARCHAR(30));
CREATE TABLE ITEM (ID INTEGER NOT NULL PRIMARY KEY, CATEGORY_ID INTEGER REFERENCES CATEGORY (ID),
  NAME VARCHAR(40), PRICE NUMERIC(10,2), NOTE BLOB SUB_TYPE TEXT);
CREATE TABLE LOG_LINES (MSG VARCHAR(50));
COMMIT;
INSERT INTO CATEGORY VALUES (1, 'tools');
INSERT INTO ITEM VALUES (1, 1, 'hammer', 10.50, 'heavy');
INSERT INTO ITEM VALUES (2, 1, 'saw', 20.00, NULL);
COMMIT;
`;
const SRC_ROWS = `
INSERT INTO CATEGORY VALUES (2, 'garden');
INSERT INTO ITEM VALUES (3, 2, 'rake', 7.25, 'new line
inside');
UPDATE ITEM SET PRICE = 12.00, NOTE = 'heavier' WHERE ID = 1;
COMMIT;
`;
const TGT_ROWS = `
INSERT INTO ITEM VALUES (9, 1, 'obsolete', 1, NULL);
COMMIT;
`;

test.beforeAll(async ({ browser, request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  for (const [name, rows] of [[SRC, SRC_ROWS], [TGT, TGT_ROWS]]) {
    const path = `${FB.dir}/fbadmin_e2e_${name}_${Date.now().toString(36)}.fdb`;
    const db = await request.post(`/api/servers/${serverId}/databases/create`, { data: { name, path } });
    expect(db.status(), await db.text()).toBe(201);
    const id = (await db.json()).id;
    dbIds.push(id);
    for (const sql of [DDL, rows]) {
      const r = await (await request.post(`/api/servers/${serverId}/databases/${id}/execute`, { data: { sql } })).json();
      expect(r.error, JSON.stringify(r.error)).toBeNull();
    }
  }
  page = await (await browser.newContext({ acceptDownloads: true })).newPage();
  await page.goto("/");
  await expand(page, SERVER);
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  for (const id of dbIds) await request.post(`/api/servers/${serverId}/databases/${id}/drop`);
  if (serverId) await request.delete(`/api/servers/${serverId}`);
});

const pane = () => page.locator(".tab-pane:not(.hidden)");

async function audit(what: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
  expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), what).toEqual([]);
}

/** Click a context-menu item, opening the submenus of long (grouped) menus as needed. */
async function menu(rowLabel: string, item: string) {
  await treeRow(page, rowLabel).click({ button: "right" });
  const ctx = page.locator(".ctx").first();
  await expect(ctx).toBeVisible();
  const target = page.locator(".ctx button[role=menuitem]", { hasText: item }).first();
  if (!(await target.isVisible())) {
    for (const sub of await ctx.locator(":scope > .ctx-sub > button").all()) {
      await sub.click();
      if (await target.isVisible()) break;
    }
  }
  await target.click();
}

test("data compare: tables, differences and sample rows", async () => {
  await menu(SRC, "Compare data…");
  await expect(pane().getByLabel("Target database")).toHaveValue(dbIds[1]);
  const tables = pane().locator(".dc-tables");
  await expect(tables).toContainText("CATEGORY");
  await expect(tables).toContainText("ITEM");
  await pane().locator("summary", { hasText: "Not comparable (1)" }).click();
  await expect(pane()).toContainText("LOG_LINES — No primary key or unique constraint");
  await audit("data compare: table selection");

  await pane().locator("button.btn.primary", { hasText: "Compare 2 table(s)" }).click();
  await expect(pane()).toContainText("Sync script: 4 statement(s)", { timeout: 30_000 });
  const item = pane().locator(".dc-results tr", { hasText: "ITEM" }).filter({ hasNotText: "CATEGORY" });
  await expect(item.locator("td").nth(1)).toHaveText("1"); // only in source
  await expect(item.locator("td").nth(2)).toHaveText("1"); // different
  await expect(item.locator("td").nth(3)).toHaveText("1"); // only in target
  await expect(item).toContainText("PRICE, NOTE");

  await pane().getByRole("button", { name: "Show rows of ITEM" }).click();
  const details = pane().locator(".dc-details");
  await expect(details.locator("td.dc-new", { hasText: "12.00" })).toBeVisible();
  await expect(details.locator("td.dc-old", { hasText: "10.50" })).toBeVisible();
  await details.getByRole("button", { name: /Only in target \(DELETE\)/ }).click();
  await expect(details.locator("td.dc-old", { hasText: "obsolete" })).toBeVisible();
  await audit("data compare: results and sample rows");
});

test("data compare: download the script, apply it, compare again", async () => {
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    pane().locator("a.btn", { hasText: "Download script" }).click(),
  ]);
  const script = readFileSync((await download.path())!, "utf8");
  expect(script.indexOf("INSERT INTO CATEGORY")).toBeLessThan(script.indexOf("INSERT INTO ITEM"));
  expect(script).toContain("DELETE FROM ITEM WHERE ID = 9;");
  expect(script).toContain("UPDATE ITEM SET PRICE = 12.00, NOTE = 'heavier' WHERE ID = 1;");

  await pane().locator("button", { hasText: "Apply to target…" }).click();
  const dlg = page.locator(".modal");
  await expect(dlg).toContainText("in one transaction");
  const apply = dlg.locator("button", { hasText: "Apply to target" });
  await expect(apply).toBeDisabled(); // the database name must be typed first
  await dlg.locator("input").fill(TGT);
  await audit("data compare: apply confirmation");
  await apply.click();
  await expect(dlg).toBeHidden();
  await expect(pane().locator(".pill.finished")).toHaveCount(2, { timeout: 30_000 }); // compare + apply jobs
  await pane().locator("button", { hasText: "Compare again" }).click();
  await expect(pane()).toContainText("Sync script: 0 statement(s)", { timeout: 30_000 });
  await expect(pane().locator(".dc-results tbody tr", { hasText: "identical" })).toHaveCount(2);
});
