import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { expand, FB, SCHEMA, treeRow } from "./fixtures";

/**
 * Milestone 2.0: visual query builder — tables from the picker and the Object Explorer (drag and
 * drop), joins from foreign keys and by dragging columns, output columns, aggregates, criteria with
 * parameters, live SQL, Run, Open in Query Tool, saved builder state, axe in all three themes.
 */
test.describe.configure({ mode: "serial" });

const SERVER = "E2E Query builder";
const SHOTS = process.env.E2E_SHOTS;
let page: Page;
let serverId = "";
let dbId = "";
let fb6 = false;

const MORE = `
CREATE TABLE ORDERS (ID INTEGER NOT NULL PRIMARY KEY, CUSTOMER_ID INTEGER NOT NULL REFERENCES CUSTOMER (ID),
  AMOUNT NUMERIC(12,2), "POSITION" INTEGER);
INSERT INTO ORDERS VALUES (1, 1, 100, 1);
INSERT INTO ORDERS VALUES (2, 1, 50, 2);
INSERT INTO ORDERS VALUES (3, 2, 70, 1);
`;

test.beforeAll(async ({ browser, request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, {
    data: { name: "qb", path: `${FB.dir}/fbadmin_e2eqb_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  const r = await (await request.post(`/api/servers/${serverId}/databases/${dbId}/execute`, { data: { sql: SCHEMA + MORE } })).json();
  expect(r.error, JSON.stringify(r.error)).toBeNull();
  fb6 = (await (await request.get(`/api/servers/${serverId}/databases/${dbId}/schemas`)).json()).supported;
  page = await (await browser.newContext()).newPage();
  await page.goto("/");
  await page.evaluate(() => { for (const k of Object.keys(localStorage)) if (k.startsWith("fbadmin.qb.")) localStorage.removeItem(k); });
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  if (!serverId) return;
  await request.post(`/api/servers/${serverId}/databases/${dbId}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

const qb = () => page.locator(".tab-pane:not(.hidden) .qb");
const sqlText = () => qb().locator(".qb-sql-text");
const T = (name: string) => (fb6 ? `PUBLIC.${name}` : name);

async function audit(what: string) {
  for (const theme of ["light", "dark", "contrast"]) {
    await page.evaluate((th) => { document.documentElement.dataset.theme = th; }, theme);
    const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
    const summary = r.violations.map((v) => `${v.id} (${v.impact}): ${v.nodes.slice(0, 3).map((n) => n.target.join(" ")).join(" | ")}`);
    expect(summary, `${what} (${theme}): axe violations`).toEqual([]);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/qb-${what.replace(/\W+/g, "-")}-${theme}.png` });
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
}

test("open from the database menu, add tables, join suggested from the foreign key", async () => {
  await expand(page, SERVER);
  await treeRow(page, "qb").click({ button: "right" });
  await page.locator(".ctx button", { hasText: "Query builder" }).click();
  await expect(qb()).toBeVisible();
  await expect(qb().locator(".qb-hint")).toBeVisible();
  const picker = qb().getByRole("combobox", { name: "Add table", exact: true });
  await picker.selectOption("table:CUSTOMER");
  await qb().getByRole("button", { name: "Add", exact: true }).click();
  await expect(qb().locator(".qb-box")).toHaveCount(1);
  await expect(sqlText()).toHaveText(`SELECT *\nFROM ${T("CUSTOMER")} C`);
  await picker.selectOption("table:ORDERS");
  await qb().getByRole("button", { name: "Add", exact: true }).click();
  await expect(qb().locator(".qb-box")).toHaveCount(2);
  await expect(qb().locator(".qb-join-label")).toHaveText("INNER");
  await expect(sqlText()).toHaveText(`SELECT C.*, O.*\nFROM ${T("CUSTOMER")} C\nINNER JOIN ${T("ORDERS")} O ON C.ID = O.CUSTOMER_ID`);
});

test("columns, aggregate with GROUP BY, criteria with a parameter, sort, limit; Run", async () => {
  const cust = qb().locator(".qb-box", { has: page.locator(".qb-alias", { hasText: /^C$/ }) });
  await cust.getByRole("checkbox", { name: "NAME" }).check();
  const ord = qb().locator(".qb-box", { has: page.locator(".qb-alias", { hasText: /^O$/ }) });
  await ord.getByRole("checkbox", { name: "POSITION" }).check();
  // aggregate and alias in the Columns grid
  await qb().getByRole("tab", { name: /Columns/ }).click();
  await qb().getByLabel("Aggregate of column 2").selectOption("COUNT");
  await qb().getByLabel("Alias of column 2").fill("orders");
  await qb().getByRole("button", { name: "Add column" }).click();
  await qb().getByLabel("Column 3", { exact: true }).selectOption({ label: "O.AMOUNT" });
  await qb().getByLabel("Aggregate of column 3").selectOption("SUM");
  await qb().getByLabel("Alias of column 3").fill("total");
  await qb().getByLabel("Sort of column 3").selectOption("DESC");
  // join type from the canvas label
  await qb().locator(".qb-join-label").click();
  await expect(qb().getByRole("tab", { name: /Joins/ })).toHaveAttribute("aria-selected", "true");
  await qb().getByLabel("Type of join 1").selectOption("LEFT");
  // criteria: WHERE with a parameter and HAVING
  await qb().getByRole("tab", { name: /Conditions/ }).click();
  await qb().getByRole("button", { name: "Add condition" }).click();
  await qb().getByLabel("Condition 1: column").selectOption({ label: "C.CITY" });
  await qb().getByLabel("Condition 1: operator").selectOption("<>");
  await qb().getByLabel("Condition 1: value").fill(":city");
  await qb().getByRole("button", { name: "Add condition" }).click();
  await qb().getByLabel("Condition 2: column").selectOption({ label: "O.AMOUNT" });
  await qb().getByLabel("Condition 2: aggregate").selectOption("SUM");
  await qb().getByLabel("Condition 2: operator").selectOption(">");
  await qb().getByLabel("Condition 2: value").fill("10");
  await qb().getByRole("spinbutton", { name: "Rows" }).fill("5");
  await expect(sqlText()).toHaveText([
    "SELECT",
    "  C.NAME,",
    '  COUNT(O."POSITION") AS ORDERS,',
    "  SUM(O.AMOUNT) AS TOTAL",
    `FROM ${T("CUSTOMER")} C`,
    `LEFT JOIN ${T("ORDERS")} O ON C.ID = O.CUSTOMER_ID`,
    "WHERE C.CITY <> :city",
    "GROUP BY C.NAME",
    "HAVING SUM(O.AMOUNT) > 10",
    "ORDER BY SUM(O.AMOUNT) DESC",
    "FETCH NEXT 5 ROWS ONLY",
  ].join("\n"));
  await qb().locator(".qb-params input").fill("Prague");
  await audit("builder");
  await qb().getByRole("button", { name: "Run", exact: true }).click();
  const grid = qb().locator(".qb-pane table.grid");
  await expect(grid).toContainText("Ann");
  await expect(grid.locator("tbody tr:has(td)")).toHaveCount(2); // Ann (150), Bob (70); Cid is in Prague
  await expect(grid.locator("tbody tr:has(td)").first()).toContainText("150");
});

test("drag a view from the Object Explorer and join by dragging a column", async () => {
  await expand(page, "qb");
  if (fb6) await expand(page, "PUBLIC");
  await expand(page, "Views");
  const canvas = qb().locator(".qb-canvas");
  await treeRow(page, "V_RICH").dragTo(canvas, { targetPosition: { x: 200, y: 330 } });
  const view = qb().locator(".qb-box", { hasText: "V_RICH" });
  await expect(view).toBeVisible();
  await expect(view.locator(".qb-alias")).toHaveText("VR");
  await expect(qb().locator(".qb-issues")).toContainText("V_RICH is not joined");
  const idCol = { has: page.locator(".qb-cname", { hasText: /^ID$/ }) };
  const from = view.locator(".qb-col", idCol);
  const to = qb().locator(".qb-box", { has: page.locator(".qb-alias", { hasText: /^C$/ }) }).locator(".qb-col", idCol);
  await from.dragTo(to);
  await expect(qb().locator(".qb-join-label")).toHaveCount(2);
  await expect(sqlText()).toContainText(`INNER JOIN ${T("V_RICH")} VR ON VR.ID = C.ID`);
  await expect(qb().locator(".qb-issues")).toHaveCount(0);
  // remove it again with the keyboard-accessible button
  await qb().getByRole("button", { name: "Remove VR" }).click();
  await expect(qb().locator(".qb-box")).toHaveCount(2);
});

test("Open in Query Tool with the builder state, and load it back", async () => {
  await qb().getByRole("checkbox", { name: /Save builder state in the SQL/ }).check();
  await qb().getByRole("button", { name: "Open in Query Tool" }).click();
  const qt = page.locator(".tab-pane:not(.hidden) .qt");
  await expect(qt.locator(".cm-content")).toContainText("LEFT JOIN");
  await expect(qt.locator(".cm-content")).toContainText("-- fbadmin:query-builder ");
  const sql = await qt.locator(".cm-content").evaluate((el) => Array.from(el.querySelectorAll(".cm-line")).map((l) => l.textContent).join("\n"));
  // back to the builder: clear it and load the saved state
  await page.locator(".tab", { hasText: "query builder" }).click();
  page.once("dialog", (d) => d.accept());
  await qb().getByRole("button", { name: "Clear" }).click();
  await expect(qb().locator(".qb-box")).toHaveCount(0);
  await qb().getByRole("button", { name: "Load from SQL…" }).click();
  const dlg = page.getByRole("dialog", { name: "Load builder state from SQL" });
  await dlg.locator("textarea").fill("SELECT 1 FROM RDB$DATABASE");
  await dlg.getByRole("button", { name: "Load" }).click();
  await expect(dlg.locator("[role=alert]")).toContainText("no saved builder state");
  await dlg.locator("textarea").fill(sql);
  await dlg.getByRole("button", { name: "Load" }).click();
  await expect(dlg).toHaveCount(0);
  await expect(qb().locator(".qb-box")).toHaveCount(2);
  await expect(sqlText()).toContainText("HAVING SUM(O.AMOUNT) > 10");
  await audit("loaded");
});

test("keyboard: build a query with the grids only", async () => {
  page.once("dialog", (d) => d.accept());
  await qb().getByRole("button", { name: "Clear" }).click();
  const picker = qb().getByRole("combobox", { name: "Add table", exact: true });
  await picker.focus();
  await picker.selectOption("view:V_RICH");
  await page.keyboard.press("Tab");
  await page.keyboard.press("Enter"); // "Add"
  await expect(qb().locator(".qb-box")).toHaveCount(1);
  await qb().getByRole("tab", { name: /Columns/ }).click();
  await qb().getByRole("button", { name: "Add column" }).focus();
  await page.keyboard.press("Enter");
  await qb().getByLabel("Column 1", { exact: true }).selectOption({ label: "(expression)" });
  await qb().getByLabel("Expression of column 1").fill("COUNT(*)");
  await qb().getByLabel("Alias of column 1").fill("n");
  await expect(sqlText()).toHaveText(`SELECT COUNT(*) AS N\nFROM ${T("V_RICH")} VR`);
  await qb().getByRole("button", { name: "Run", exact: true }).click();
  await expect(qb().locator(".qb-pane table.grid tbody")).toContainText("1");
});

test("Firebird 6: a table of another schema is schema-qualified", async ({ request }) => {
  test.skip(!fb6, "needs Firebird 6 (FB_PORT of a Firebird 6 server)");
  const r = await (await request.post(`/api/servers/${serverId}/databases/${dbId}/execute`, {
    data: { sql: "CREATE SCHEMA APP; CREATE TABLE APP.ITEMS (ID INTEGER, NAME VARCHAR(10)); INSERT INTO APP.ITEMS VALUES (1, 'from app');" },
  })).json();
  expect(r.error, JSON.stringify(r.error)).toBeNull();
  page.once("dialog", (d) => d.accept());
  await qb().getByRole("button", { name: "Clear" }).click();
  await qb().getByRole("button", { name: "Refresh the list of tables" }).click();
  await qb().getByRole("combobox", { name: "Schema", exact: true }).selectOption("APP");
  const picker = qb().getByRole("combobox", { name: "Add table", exact: true });
  await expect(picker.locator("option", { hasText: "ITEMS" })).toHaveCount(1);
  await picker.selectOption("table:ITEMS");
  await qb().getByRole("button", { name: "Add", exact: true }).click();
  await expect(sqlText()).toHaveText("SELECT *\nFROM APP.ITEMS I");
  await qb().getByRole("button", { name: "Run", exact: true }).click();
  await expect(qb().locator(".qb-pane table.grid")).toContainText("from app");
});
