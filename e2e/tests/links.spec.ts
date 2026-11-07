import AxeBuilder from "@axe-core/playwright";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { FB, expand, setEditor, treeRow } from "./fixtures";

/**
 * Roadmap 2.3: shareable links. A table's link is copied from the tree and opened in a fresh page; a Query
 * Tool result is shared as a snapshot and opened; a revoked link is refused. (backend/tests/test_links.py
 * covers server mode: links open with the viewer's own permissions.)
 */
test.describe.configure({ mode: "serial" });

const SERVER = `E2E Links ${Date.now().toString(36)}`;
let page: Page;
let serverId = "";
let dbId = "";

async function run(request: APIRequestContext, sql: string) {
  const s = await (await request.post("/api/query/sessions", { data: { server_id: serverId, database_id: dbId } })).json();
  const r = await (await request.post(`/api/query/sessions/${s.session_id}/execute`, { data: { sql, autocommit: true } })).json();
  expect(r.error, JSON.stringify(r.error)).toBeNull();
  await request.delete(`/api/query/sessions/${s.session_id}`);
}

test.beforeAll(async ({ browser, request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, {
    data: { name: "linkdb", path: `${FB.dir}/fbadmin_e2elk_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  await run(request, "CREATE TABLE CUSTOMERS (ID INTEGER NOT NULL PRIMARY KEY, CITY VARCHAR(30));");
  await run(request, "INSERT INTO CUSTOMERS VALUES (1, 'Bucharest');\nINSERT INTO CUSTOMERS VALUES (2, 'Lisbon');");
  const ctx = await browser.newContext();
  await ctx.grantPermissions(["clipboard-read", "clipboard-write"]);
  page = await ctx.newPage();
  await page.goto("/");
});

test.afterAll(async ({ request }) => {
  for (const l of (await (await request.get("/api/links")).json()).links ?? []) await request.delete(`/api/links/${l.id}`);
  await page?.context().close();
  await request.post(`/api/servers/${serverId}/databases/${dbId}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

const pane = () => page.locator(".tab-pane:not(.hidden)");
const clipboard = () => page.evaluate(() => navigator.clipboard.readText());

async function audit(what: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
  expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), what).toEqual([]);
}

test("copy a table's link and open it", async () => {
  await expand(page, SERVER);
  await expand(page, "linkdb");
  await expand(page, "Tables");
  await treeRow(page, "CUSTOMERS").click({ button: "right" });
  await page.locator(".ctx button[role=menuitem]", { hasText: "Copy link" }).first().click();
  await expect(page.locator(".toast").last()).toContainText("Link copied");
  const url = await clipboard();
  expect(url).toMatch(/\/#\/link\/[A-Za-z0-9_-]{10,}$/);

  const other = await page.context().newPage();
  await other.goto(url);
  await expect(other.locator(".toast").last()).toContainText("Opened CUSTOMERS from a link", { timeout: 10_000 });
  await expect(other.getByText("Table CUSTOMERS", { exact: true })).toBeVisible();
  expect(new URL(other.url()).hash).toBe("");  // the link is taken out of the address bar
  await other.close();
});

test("share a result as a snapshot", async () => {
  await treeRow(page, "linkdb").click();
  await page.locator(".topbar button", { hasText: "Query Tool" }).click();
  const qt = page.locator(".tab-pane:not(.hidden) .qt");
  await setEditor(page, qt, "SELECT ID, CITY FROM CUSTOMERS ORDER BY ID");
  await qt.locator("button", { hasText: "Execute" }).click();
  await expect(qt.locator(".qt-out table")).toContainText("Lisbon");
  await qt.getByRole("button", { name: /Share/ }).click();
  await qt.getByLabel("Title").fill("Customer cities");
  await qt.getByLabel("Expires").selectOption("7");
  await audit("share a result");
  await qt.getByRole("button", { name: "Create and copy the link" }).click();
  // the field shows the new link; the clipboard gets the same one (an earlier toast may still be up)
  await expect(qt.getByLabel("Link")).toHaveValue(/\/#\/link\/[A-Za-z0-9_-]{10,}$/);
  const url = await qt.getByLabel("Link").inputValue();
  await expect.poll(clipboard).toBe(url);

  await page.goto(url);
  const snap = pane().locator(".lk-snap");
  await expect(snap.getByRole("heading", { name: "Customer cities" })).toBeVisible({ timeout: 10_000 });
  await expect(snap).toContainText("linkdb on " + SERVER);
  await expect(snap.getByRole("region", { name: "Shared rows" })).toContainText("Bucharest");
  await expect(snap.getByLabel("SQL")).toContainText("SELECT ID, CITY FROM CUSTOMERS");
  for (const theme of ["light", "dark", "contrast"]) {
    await page.evaluate((th) => { document.documentElement.dataset.theme = th; }, theme);
    await audit(`snapshot (${theme} theme)`);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
});

test("the links list; a revoked link is refused", async () => {
  await page.locator(".topbar button", { hasText: "Library" }).click();
  await pane().getByRole("button", { name: "🔗 Links" }).click();
  const list = pane().getByRole("region", { name: "Shared links" });
  await expect(list.locator("tbody tr")).toHaveCount(2);
  const row = list.locator("tr", { hasText: "Customer cities" });
  await expect(row).toContainText("result snapshot");
  await expect(row.locator("td").nth(6)).toHaveText("1");
  await audit("links list");
  await row.getByRole("button", { name: "Show link" }).click();
  const url = await page.getByRole("dialog").getByLabel("Link").inputValue();
  await page.keyboard.press("Escape");
  page.once("dialog", (d) => d.accept());
  await row.getByRole("button", { name: "Revoke link" }).click();
  await expect(list.locator("tbody tr")).toHaveCount(1);
  await page.goto(url);
  await expect(page.getByRole("dialog")).toContainText("This link does not exist, has expired or was revoked", { timeout: 10_000 });
});
