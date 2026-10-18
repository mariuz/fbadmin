import { expect, test, type Page } from "@playwright/test";
import { expand, FB, field, SCHEMA, setEditor, treeRow } from "./fixtures";

/**
 * Smoke tests of the feature modules (milestones 0.3–0.8): each one is opened from the tree's
 * context menu, does one real round trip to Firebird, and shows the result.
 */
test.describe.configure({ mode: "serial" });

const SERVER = "E2E Features";
let page: Page;
let serverId = "";
let dbId = "";
const DB_LABEL = "features";

test.beforeAll(async ({ browser, request }) => {
  const srv = await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  });
  serverId = (await srv.json()).id;
  const path = `${FB.dir}/fbadmin_e2ef_${Date.now().toString(36)}.fdb`;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, { data: { name: DB_LABEL, path } });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  const r = await (await request.post(`/api/servers/${serverId}/databases/${dbId}/execute`, {
    data: { sql: SCHEMA + "\nSET TERM ^ ;\nCREATE PROCEDURE P_SUM (N INTEGER) RETURNS (S INTEGER) AS\nDECLARE I INTEGER = 0;\nBEGIN\n  S = 0;\n  WHILE (I < N) DO\n  BEGIN\n    S = S + I;\n    I = I + 1;\n  END\n  SUSPEND;\nEND^\nSET TERM ; ^\n" },
  })).json();
  expect(r.error, JSON.stringify(r.error)).toBeNull();
  page = await browser.newPage();
  await page.goto("/");
  await expand(page, SERVER);
  await expand(page, DB_LABEL);
});

test.afterAll(async ({ request }) => {
  await page?.close();
  if (!serverId) return;
  await request.post(`/api/servers/${serverId}/databases/${dbId}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

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

const pane = () => page.locator(".tab-pane:not(.hidden)");

test("table dialogs: add a column through the object dialog", async () => {
  await expand(page, "Tables");
  await menu("CUSTOMER", "Add column…");
  const dlg = page.locator(".modal");
  await expect(dlg).toBeVisible();
  await field(dlg, "Name").locator("input").first().fill("EMAIL");
  await dlg.locator("button.btn.primary", { hasText: /^(Save|Create)$/ }).click();
  await expect(dlg).toBeHidden();
  await treeRow(page, "CUSTOMER").locator(".twisty").click(); // collapse
  await treeRow(page, "CUSTOMER").locator(".twisty").click(); // expand again
  await expect(treeRow(page, "EMAIL")).toBeVisible();
});

test("schema tools: full DDL extract", async () => {
  await menu(DB_LABEL, "Extract DDL (full script)");
  await pane().locator("button", { hasText: "Extract" }).first().click();
  await expect(pane()).toContainText("CREATE TABLE CUSTOMER");
  await expect(pane()).toContainText("CREATE PROCEDURE P_SUM");
});

test("schema tools: ERD shows tables", async () => {
  await menu(DB_LABEL, "ERD diagram");
  await expect(pane()).toContainText("CUSTOMER");
});

test("schema tools: unused / invalid objects report", async () => {
  await menu(DB_LABEL, "Unused / invalid objects");
  await expect(pane().locator("h3").first()).toBeVisible();
});

test("maintenance: gstat statistics charts", async () => {
  await menu(DB_LABEL, "Statistics charts (gstat)");
  await pane().locator("button", { hasText: "Analyze database" }).click();
  await expect(pane()).toContainText("CUSTOMER", { timeout: 30_000 });
  await expect(pane()).toContainText("Indices");
});

test("monitoring: performance dashboard and index advisor", async () => {
  await menu(DB_LABEL, "Performance dashboard");
  await expect(pane()).toContainText(/Attachments|attachments/);
  await menu(DB_LABEL, "Index advisor"); // reuses the open tab and switches to its advisor view
  await pane().locator("button", { hasText: "Analyze" }).first().click();
  await expect(pane()).toContainText("Analyzed statements");
});

test("monitoring: profiler runs a procedure and shows PSQL hot spots", async () => {
  await menu(DB_LABEL, "Profiler");
  const editorScope = pane();
  await setEditor(page, editorScope, "SELECT * FROM P_SUM(5000)");
  await pane().locator("button", { hasText: "Profile (F5)" }).click();
  await expect(pane()).toContainText("PSQL hot spots", { timeout: 30_000 });
  await expect(pane()).toContainText("P_SUM");
});

test("replication: publication panel opens", async () => {
  await menu(DB_LABEL, "Replication");
  await expect(pane()).toContainText(/[Pp]ublication/);
  await expect(pane()).toContainText("Current journal segment");
});

test("security: Firebird users via the Services API", async () => {
  await menu(SERVER, "Firebird users (Services API)…");
  await expect(pane()).toContainText("SYSDBA");
});
