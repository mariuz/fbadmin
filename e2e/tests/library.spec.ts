import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { expand, FB, SCHEMA, setEditor, treeRow } from "./fixtures";

/**
 * Milestone 2.0 query library: browser snippets are imported on first use, "Save to library" from the
 * Query Tool, search, open/run on the item's database (with :name parameters), edit and revisions.
 */
test.describe.configure({ mode: "serial" });

const SERVER = "E2E Library";
const DB = "libdb";
let page: Page;
let serverId = "";
let dbId = "";

test.beforeAll(async ({ browser, request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, {
    data: { name: DB, path: `${FB.dir}/fbadmin_e2el_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  await request.post(`/api/servers/${serverId}/databases/${dbId}/execute`, { data: { sql: SCHEMA } });
  page = await (await browser.newContext()).newPage();
  await page.goto("/");
  // a snippet saved in this browser by the Query Tool (older fbadmin versions kept only these)
  await page.evaluate(() => {
    localStorage.setItem("fbadmin.snippets", JSON.stringify([{ name: "Legacy snippet", sql: "SELECT 42 AS ANSWER FROM RDB$DATABASE" }]));
    localStorage.removeItem("fbadmin.library.snippetsImported");
  });
  await page.reload();
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  for (const it of await (await request.get("/api/library/items")).json()) await request.delete(`/api/library/items/${it.id}`);
  if (!serverId) return;
  await request.post(`/api/servers/${serverId}/databases/${dbId}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

const pane = () => page.locator(".tab-pane:not(.hidden)");

async function audit(what: string) {
  for (const theme of ["", "dark", "contrast"]) {
    await page.evaluate((th) => { if (th) document.documentElement.dataset.theme = th; else delete document.documentElement.dataset.theme; }, theme);
    const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
    expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), `${what} (${theme || "light"})`).toEqual([]);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
}

test("browser snippets are imported into the library on first use", async () => {
  await page.locator(".topbar button", { hasText: "Library" }).click();
  const lib = pane().locator(".lib");
  await expect(lib.locator(".lib-item", { hasText: "Legacy snippet" })).toBeVisible();
  await expect(lib.locator(".lib-nav")).toContainText("Snippets");
  // still in the browser too: the Query Tool's Snippets pane keeps working
  expect(await page.evaluate(() => localStorage.getItem("fbadmin.snippets"))).toContain("Legacy snippet");
  await audit("library (empty selection)");
});

test("save the Query Tool's editor to the library", async () => {
  await expand(page, SERVER);
  await treeRow(page, DB).click();
  await page.locator(".topbar button", { hasText: "Query Tool" }).click();
  const qt = pane().locator(".qt");
  await setEditor(page, qt, "SELECT NAME FROM CUSTOMER WHERE CITY = :city");
  await qt.getByRole("button", { name: "Save to library", exact: true }).click();
  const dlg = page.getByRole("dialog", { name: "Save to library" });
  await expect(dlg).toBeVisible();
  await expect(dlg.getByLabel("Default database")).toHaveValue(`${serverId}/${dbId}`);
  await dlg.getByLabel("Name", { exact: true }).fill("Customers in a city");
  await dlg.getByLabel("Description").fill("Who lives where");
  await dlg.getByLabel("Folder").fill("Reports/Customers");
  await dlg.getByLabel("Tags (comma-separated)").fill("report, customers");
  await expect(dlg.locator(".lib-preview")).toHaveText("SELECT NAME FROM CUSTOMER WHERE CITY = :city");
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).include(".modal").analyze();
  expect(r.violations.map((v) => v.id), "save dialog").toEqual([]);
  await dlg.locator("button.primary", { hasText: "Save" }).click();
  await expect(dlg).toBeHidden();
  await expect(page.locator(".toast", { hasText: "Saved to the library" })).toBeVisible();
});

test("find it in the library and run it on its database", async () => {
  await page.locator(".topbar button", { hasText: "Library" }).click();
  const lib = pane().locator(".lib");
  await lib.getByLabel("Search the library").fill("city = :city");
  await expect(lib.locator(".lib-item")).toHaveCount(1);
  await lib.locator(".lib-item", { hasText: "Customers in a city" }).click();
  const detail = lib.locator(".lib-detail");
  await expect(detail.locator("h3")).toHaveText("Customers in a city");
  await expect(detail).toContainText("Who lives where");
  await expect(detail).toContainText(`${SERVER} / ${DB}`);
  await expect(detail.locator(".lib-param")).toHaveText(":CITY");
  // tag and folder filters
  await lib.getByLabel("Search the library").fill("");
  await lib.locator(".lib-tag", { hasText: "#report" }).click();
  await expect(lib.locator(".lib-item")).toHaveCount(1);
  await lib.locator(".lib-tag", { hasText: "#report" }).click();
  await lib.locator(".lib-nav button", { hasText: "Customers" }).click();
  await expect(lib.locator(".lib-item")).toHaveCount(1);
  await lib.locator(".lib-nav button", { hasText: "All folders" }).click();
  await audit("library with a selected item");

  await detail.locator("button", { hasText: "Run" }).click();
  const params = page.locator(".modal", { hasText: "Query parameters" });
  await expect(params).toBeVisible();
  await params.locator("input.text").first().fill("Lisbon");
  await params.locator("button", { hasText: "Execute" }).click();
  const qt = pane().locator(".qt");
  await expect(qt.locator(".cm-content")).toContainText("WHERE CITY = :city");
  await qt.locator(".subtabs button", { hasText: "Data output" }).click();
  await expect(qt.locator(".qt-out table")).toContainText("Bob");
});

test("edit keeps revisions; an older one can be viewed and restored", async () => {
  await page.locator(".topbar button", { hasText: "Library" }).click();
  const lib = pane().locator(".lib");
  await lib.locator(".lib-item", { hasText: "Customers in a city" }).click();
  const detail = lib.locator(".lib-detail");
  await detail.locator("button", { hasText: "Edit" }).click();
  await detail.getByLabel("Name", { exact: true }).fill("Customers by city");
  await setEditor(page, detail, "SELECT NAME, CITY FROM CUSTOMER ORDER BY CITY");
  await detail.locator("button.primary", { hasText: "Save" }).click();
  await expect(detail.locator("h3")).toHaveText("Customers by city");
  await expect(detail.locator(".cm-content")).toContainText("ORDER BY CITY");
  await detail.locator("button", { hasText: "History (2)" }).click();
  const rows = detail.locator("table.grid tbody tr");
  await expect(rows).toHaveCount(2);
  await rows.nth(1).locator("button", { hasText: "View" }).click();
  await expect(detail.locator(".cm-content")).toContainText("WHERE CITY = :city");
  await audit("library revision history");
  await rows.nth(1).locator("button", { hasText: "Restore" }).click();
  await expect(detail.locator("h3")).toHaveText("Customers in a city");
  await expect(detail).toContainText("Revision 3");
  // a copy is a new item
  page.once("dialog", (d) => d.accept("My copy"));
  await detail.locator("button", { hasText: "Save a copy" }).click();
  await expect(lib.locator(".lib-item", { hasText: "My copy" })).toBeVisible();
  await expect(detail.locator("h3")).toHaveText("My copy");
});
