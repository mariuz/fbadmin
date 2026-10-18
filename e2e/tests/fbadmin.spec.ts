import { expect, test, type Page } from "@playwright/test";
import { createTestDatabase, dropTestDatabase, expand, FB, field, setEditor, treeRow } from "./fixtures";

// One fbadmin instance, one database: the steps build on each other (register -> browse -> ...).
test.describe.configure({ mode: "serial" });

let dbPath = "";
let dbLabel = "";
let page: Page;
const SERVER = "E2E Firebird";

test.beforeAll(async ({ browser, request }) => {
  dbPath = await createTestDatabase(request);
  dbLabel = dbPath.split("/").pop()!.replace(/\.fdb$/, "");
  page = await browser.newPage();
});

test.afterAll(async ({ request }) => {
  await page?.close();
  if (dbPath) await dropTestDatabase(request, dbPath);
});

/** Select the test database in the tree (expanding the server if needed). */
async function selectDatabase() {
  const db = treeRow(page, dbLabel);
  if (!(await db.isVisible())) await expand(page, SERVER);
  await db.click();
}

/** Open a Query Tool on the test database and return its pane. */
async function openQueryTool() {
  await selectDatabase();
  await page.locator(".topbar button", { hasText: "Query Tool" }).click();
  const qt = page.locator(".tab-pane:not(.hidden) .qt");
  await expect(qt).toBeVisible();
  return qt;
}

test("starts empty and makes no update-check request unless opted in", async () => {
  const calls: string[] = [];
  page.on("request", (r) => r.url().includes("/api/version/latest") && calls.push(r.url()));
  await page.goto("/");
  await expect(page.locator(".brand")).toContainText("fbadmin");
  await expect(page.locator(".tree")).toContainText("No servers registered yet.");
  await page.waitForTimeout(500);
  expect(calls).toEqual([]);
});

test("registers a server with its first database", async () => {
  await page.locator(".topbar button", { hasText: "Add server" }).click();
  const dlg = page.locator(".modal");
  await expect(dlg.locator(".modal-head")).toHaveText("Register server");
  await field(dlg, "Name").fill(SERVER);
  await field(dlg, "Host").fill(FB.host);
  await field(dlg, "Port").fill(String(FB.port));
  await field(dlg, "User").fill(FB.user);
  await field(dlg, "Password").fill(FB.password);
  await field(dlg, "First database").fill(dbPath);
  await dlg.locator("button", { hasText: "Register" }).click();
  await expect(dlg).toBeHidden();
  await expect(page.locator(".toast")).toContainText("Server registered");
  await expect(treeRow(page, SERVER)).toBeVisible();
});

test("browses the object tree: server, database, folders, table columns", async () => {
  await expand(page, SERVER);
  await expect(treeRow(page, dbLabel)).toBeVisible();
  await expand(page, dbLabel);
  const tables = treeRow(page, "Tables");
  await expect(tables).toContainText("(1)");
  await expect(treeRow(page, "Views")).toContainText("(1)");
  await expand(page, "Tables");
  await expect(treeRow(page, "CUSTOMER")).toBeVisible();
  await expand(page, "CUSTOMER");
  await expect(treeRow(page, "BALANCE")).toBeVisible();
  // dashboard of the database shows header info
  await treeRow(page, dbLabel).click();
  await expect(page.locator(".tab.active")).toHaveText("Dashboard");
});

test("shows properties and re-creatable DDL of a table", async () => {
  await treeRow(page, "CUSTOMER").click();
  await expect(page.locator(".tab.active")).toHaveText("Properties");
  const pane = page.locator(".tab-pane:not(.hidden)");
  await expect(pane).toContainText("Columns");
  await expect(pane).toContainText("CITY");
  await page.locator(".tab", { hasText: /^SQL$/ }).click();
  const ddl = page.locator(".tab-pane:not(.hidden) .cm-content");
  await expect(ddl).toContainText("CREATE TABLE CUSTOMER");
  await expect(ddl).toContainText("PRIMARY KEY");
  await expect(ddl).toContainText("CREATE INDEX IX_CUSTOMER_CITY");
  await page.locator(".tab", { hasText: "Dependencies" }).click();
  await expect(page.locator(".tab-pane:not(.hidden)")).toContainText("V_RICH");
});

test("runs a query in the Query Tool and shows the result grid", async () => {
  const qt = await openQueryTool();
  await setEditor(page, qt, "SELECT ID, NAME, CITY FROM CUSTOMER ORDER BY ID");
  await qt.locator("button", { hasText: "Execute" }).click();
  const grid = qt.locator(".qt-out table");
  await expect(grid).toContainText("Bucharest");
  await expect(grid.locator("tbody tr")).toHaveCount(3);
  await expect(qt.locator(".status")).toContainText("3 rows");
  // messages pane lists the statement
  await qt.locator(".subtabs button", { hasText: "Messages" }).click();
  await expect(qt.locator(".messages")).toContainText("SELECT ID, NAME, CITY FROM CUSTOMER");
});

test("prompts for :named parameters", async () => {
  const qt = page.locator(".tab-pane:not(.hidden) .qt");
  await setEditor(page, qt, "SELECT NAME FROM CUSTOMER WHERE CITY = :city");
  await qt.locator("button", { hasText: "Execute" }).click();
  const dlg = page.locator(".modal", { hasText: "Query parameters" });
  await expect(dlg).toBeVisible();
  await expect(dlg).toContainText(":CITY");
  await dlg.locator("input.text").first().fill("Lisbon");
  await dlg.locator("button", { hasText: "Execute" }).click();
  await qt.locator(".subtabs button", { hasText: "Data output" }).click();
  const grid = qt.locator(".qt-out table");
  await expect(grid).toContainText("Bob");
  await expect(grid.locator("tbody tr")).toHaveCount(1);
});

test("reports errors with their position", async () => {
  const qt = page.locator(".tab-pane:not(.hidden) .qt");
  await setEditor(page, qt, "SELECT 1 FROM RDB$DATABASE;\nSELECT NOPE FROM CUSTOMER;");
  await qt.locator("button", { hasText: "Execute" }).click();
  await expect(qt.locator(".status .err")).toContainText("error at line 2");
  await qt.locator(".subtabs button", { hasText: "Messages" }).click();
  await expect(qt.locator(".messages")).toContainText("NOPE");
});

test("cancels a long-running statement", async () => {
  const qt = page.locator(".tab-pane:not(.hidden) .qt");
  await setEditor(page, qt, "SELECT COUNT(*) FROM RDB$FIELDS a, RDB$FIELDS b, RDB$FIELDS c, RDB$FIELDS d");
  await qt.locator("button", { hasText: "Execute" }).click();
  const cancel = qt.locator("button.stop");
  await expect(cancel).toBeVisible();
  await page.waitForTimeout(700);
  await cancel.click();
  await expect(qt.locator("button", { hasText: "Execute" })).toBeVisible({ timeout: 30_000 });
  await qt.locator(".subtabs button", { hasText: "Messages" }).click();
  await expect(qt.locator(".messages")).toContainText(/cancel/i);
  // the session is still usable afterwards
  await setEditor(page, qt, "SELECT 42 AS ANSWER FROM RDB$DATABASE");
  await qt.locator("button", { hasText: "Execute" }).click();
  await qt.locator(".subtabs button", { hasText: "Data output" }).click();
  await expect(qt.locator(".qt-out table")).toContainText("42");
});

test("edits table data in the grid and saves it", async () => {
  await treeRow(page, "CUSTOMER").click();
  await page.locator(".topbar button", { hasText: "View data" }).click();
  const pane = page.locator(".tab-pane:not(.hidden)");
  const grid = pane.locator("table.grid");
  await expect(grid).toContainText("Prague");
  const cell = grid.locator("tbody tr").filter({ hasText: "Cid" }).locator("td", { hasText: "Prague" });
  await cell.dblclick();
  const input = grid.locator("td.sel input");
  await input.fill("Brno");
  await input.press("Enter");
  await expect(grid.locator("td.dirty")).toHaveText("Brno");
  await pane.locator("button", { hasText: "Save" }).click();
  await expect(page.locator(".toast").last()).toContainText("Saved: 1 statement(s), 1 row(s)");
  // read back through the Query Tool
  const qt = await openQueryTool();
  await setEditor(page, qt, "SELECT CITY FROM CUSTOMER WHERE ID = 3");
  await qt.locator("button", { hasText: "Execute" }).click();
  await expect(qt.locator(".qt-out table")).toContainText("Brno");
});

test("the About dialog opts in to the update check", async () => {
  await page.route("**/api/version/latest*", (route) => route.fulfill({
    json: { current: "0.0.1", enabled: true, latest: "99.0.0", url: "https://github.com/mariuz/fbadmin/releases",
            published_at: null, update_available: true, error: null },
  }));
  await page.locator(".topbar button", { hasText: "About" }).click();
  const dlg = page.locator(".modal", { hasText: "About fbadmin" });
  await expect(dlg).toContainText("no telemetry");
  await dlg.locator("label.check input").check();
  await dlg.locator("button", { hasText: "Check now" }).click();
  await expect(dlg).toContainText("99.0.0");
  await dlg.locator("button", { hasText: "Close" }).click();
  await expect(page.locator(".topbar .badge", { hasText: "Update 99.0.0" })).toBeVisible();
});
