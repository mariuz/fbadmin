import { expect, test, type Page } from "@playwright/test";
import { expand, FB, setEditor, treeRow } from "./fixtures";

/**
 * Firebird 6 SQL schemas: the tree gets a schema level and the Query Tool a schema picker.
 * Runs against the server at FB6_PORT (default 3056, e.g. `firebirdsql/firebird:6-snapshot`)
 * and is skipped when that server is missing or older than Firebird 6.
 */
test.describe.configure({ mode: "serial" });

const PORT = Number(process.env.FB6_PORT ?? 3056);
const SERVER = "E2E Firebird 6";
const DDL = `
CREATE SCHEMA APP;
CREATE TABLE APP.T1 (ID INTEGER NOT NULL PRIMARY KEY, NAME VARCHAR(10));
INSERT INTO APP.T1 VALUES (1, 'in app');
CREATE TABLE PUBLIC.T1 (X INTEGER);
CREATE TABLE PUBLIC.ONLY_PUB (X INTEGER);
`;

let page: Page;
let serverId = "";
let dbId = "";
let dbLabel = "";

test.beforeAll(async ({ browser, request }) => {
  const srv = await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: PORT, user: FB.user, password: FB.password },
  });
  serverId = (await srv.json()).id;
  const path = `${FB.dir}/fbadmin_e2e6_${Date.now().toString(36)}.fdb`;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, { data: { name: "e2e6", path } });
  if (db.status() !== 201) {
    await request.delete(`/api/servers/${serverId}`);
    serverId = "";
    test.skip(true, `no Firebird server on port ${PORT}: ${await db.text()}`);
  }
  dbId = (await db.json()).id;
  dbLabel = "e2e6";
  const sc = await (await request.get(`/api/servers/${serverId}/databases/${dbId}/schemas`)).json();
  if (!sc.supported) {
    await request.post(`/api/servers/${serverId}/databases/${dbId}/drop`);
    await request.delete(`/api/servers/${serverId}`);
    serverId = "";
    test.skip(true, "the server is older than Firebird 6");
  }
  const r = await (await request.post(`/api/servers/${serverId}/databases/${dbId}/execute`, { data: { sql: DDL } })).json();
  expect(r.error, JSON.stringify(r.error)).toBeNull();
  page = await browser.newPage();
  await page.goto("/");
});

test.afterAll(async ({ request }) => {
  await page?.close();
  if (!serverId) return;
  await request.post(`/api/servers/${serverId}/databases/${dbId}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

test("lists schemas in the tree, each with its own objects", async () => {
  await expand(page, SERVER);
  await expand(page, dbLabel);
  await expect(treeRow(page, "APP")).toBeVisible();
  await expect(treeRow(page, "PUBLIC")).toBeVisible();
  await expand(page, "APP");
  await expect(treeRow(page, "Tables")).toContainText("(1)");
  await expand(page, "Tables");
  await expect(treeRow(page, "T1")).toBeVisible();
  await expect(page.locator(".tree")).not.toContainText("ONLY_PUB");
  await treeRow(page, "T1").click();
  const pane = page.locator(".tab-pane:not(.hidden)");
  await expect(pane).toContainText("NAME");
  await page.locator(".tab", { hasText: /^SQL$/ }).click();
  const ddl = page.locator(".tab-pane:not(.hidden) .cm-content");
  await expect(ddl).toContainText("SET SEARCH_PATH TO APP");
  await expect(ddl).toContainText("CREATE TABLE T1");
});

test("the Query Tool opens in the selected schema and can switch", async () => {
  await page.locator(".topbar button", { hasText: "Query Tool" }).click();
  const qt = page.locator(".tab-pane:not(.hidden) .qt");
  await expect(qt).toBeVisible();
  await setEditor(page, qt, "SELECT NAME FROM T1");
  await qt.locator("button", { hasText: "Execute" }).click();
  await expect(qt.locator(".qt-out table")).toContainText("in app");
  const picker = qt.locator("label", { hasText: "Schema" }).locator("select");
  await expect(picker).toHaveValue("APP");
  await picker.selectOption("PUBLIC");
  await expect(picker).toHaveValue("PUBLIC");
  await setEditor(page, qt, "SELECT COUNT(*) AS N FROM ONLY_PUB");
  await qt.locator("button", { hasText: "Execute" }).click();
  await expect(qt.locator(".status")).toContainText("1 row");
});

test("data tabs keep the schema they were opened in", async () => {
  await treeRow(page, "T1").click({ button: "right" });
  await page.locator(".ctx button", { hasText: "View/Edit data" }).click();
  const pane = page.locator(".tab-pane:not(.hidden)");
  await expect(pane).toContainText("in app");
  // switching to PUBLIC elsewhere does not change the open tab
  await treeRow(page, "PUBLIC").click();
  await page.locator(".tab", { hasText: /^T1$/ }).click();
  await page.locator(".tab-pane:not(.hidden) button[title*='Refresh'], .tab-pane:not(.hidden) button:has-text('Refresh')").first().click();
  await expect(page.locator(".tab-pane:not(.hidden)")).toContainText("in app");
});
