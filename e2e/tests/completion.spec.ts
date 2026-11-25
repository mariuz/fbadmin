import { expect, test, type Page } from "@playwright/test";
import { FB, expand, setEditor, treeRow } from "./fixtures";

/**
 * Issues #2, #7, #8: Query Tool completion of Firebird names. Names are quoted only when needed, the alias of an
 * unquoted table resolves, and a prefix with `$` is replaced as a whole.
 * (frontend/tests/sqlcomplete.test.ts covers the rules.)
 */
test.describe.configure({ mode: "serial" });

const SERVER = `E2E Completion ${Date.now().toString(36)}`;
let page: Page;
let serverId = "";
let dbId = "";

test.beforeAll(async ({ browser, request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, {
    data: { name: "compdb", path: `${FB.dir}/fbadmin_e2ecomp_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  for (const sql of ["CREATE TABLE ARTIKEL (ARTIKELID INTEGER NOT NULL PRIMARY KEY, BEZEICHNUNG VARCHAR(40))",
    'CREATE TABLE "MixedCase" ("Id" INTEGER)']) {
    expect((await (await request.post(`/api/servers/${serverId}/databases/${dbId}/execute`, { data: { sql } })).json()).error).toBeNull();
  }
  page = await (await browser.newContext()).newPage();
  await page.goto("/");
  await expand(page, SERVER);
  await treeRow(page, "compdb").click();
  await page.locator(".topbar button", { hasText: "Query Tool" }).click();
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  await request.post(`/api/servers/${serverId}/databases/${dbId}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

const qt = () => page.locator(".tab-pane:not(.hidden) .qt");
const doc = () => qt().locator(".cm-content").first();

async function complete(text: string, cursorBack: number, pick: string) {
  await setEditor(page, qt(), text);
  for (let i = 0; i < cursorBack; i++) await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("Control+Space");
  const list = page.locator(".cm-tooltip-autocomplete");
  await expect(list).toBeVisible();
  // the accessible name is the label followed by its detail (the table, for a column)
  await list.getByRole("option", { name: new RegExp(`^${pick.replace(/\$/g, "\\$")}`) }).first().click();
}

test("a plain name is inserted without quotes (#8)", async () => {
  await complete("select * from arti", 0, "ARTIKEL");
  await expect(doc()).toHaveText("select * from ARTIKEL");
});

test("a mixed-case name keeps its quotes (#8)", async () => {
  await complete("select * from mixed", 0, "MixedCase");
  await expect(doc()).toHaveText('select * from "MixedCase"');
});

test("a prefix with $ is replaced as a whole (#2)", async () => {
  await complete("select * from mon$data", 0, "MON$DATABASE");
  await expect(doc()).toHaveText("select * from MON$DATABASE");
});

test("columns of an unquoted table's alias (#7)", async () => {
  const sql = "select a. from artikel a";
  await complete(sql, sql.length - "select a.".length, "BEZEICHNUNG");
  await expect(doc()).toHaveText("select a.BEZEICHNUNG from artikel a");
});
