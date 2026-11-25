import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { FB, expand, setEditor, treeRow } from "./fixtures";

/**
 * Issues #5 and #6: Fetch all next to Load more, Rows = all (execute and fetch all), and exports that never come out
 * silently partial: with the cursor open, the export menu offers all rows (fetched first) or the loaded ones.
 */
test.describe.configure({ mode: "serial" });

const SERVER = `E2E Fetch all ${Date.now().toString(36)}`;
let page: Page;
let serverId = "";
let dbId = "";

test.beforeAll(async ({ browser, request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, {
    data: { name: "manyrows", path: `${FB.dir}/fbadmin_e2efa_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  for (const sql of ["CREATE TABLE N (I INTEGER)",
    "EXECUTE BLOCK AS DECLARE I INTEGER = 0; BEGIN WHILE (I < 12345) DO BEGIN INSERT INTO N VALUES (:I); I = I + 1; END END"]) {
    expect((await (await request.post(`/api/servers/${serverId}/databases/${dbId}/execute`, { data: { sql } })).json()).error).toBeNull();
  }
  page = await (await browser.newContext({ acceptDownloads: true })).newPage();
  await page.goto("/");
  await expand(page, SERVER);
  await treeRow(page, "manyrows").click();
  await page.locator(".topbar button", { hasText: "Query Tool" }).click();
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  await request.post(`/api/servers/${serverId}/databases/${dbId}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

const qt = () => page.locator(".tab-pane:not(.hidden) .qt");

async function run(rows: string) {
  await qt().locator("label", { hasText: "Rows" }).locator("select").selectOption(rows);
  await setEditor(page, qt(), "SELECT I FROM N ORDER BY I");
  await qt().locator("button", { hasText: "Execute" }).click();
}

test("exports offer all rows or the loaded ones, then Fetch all (#5, #6)", async () => {
  await run("100");
  await expect(qt().getByText("100+ rows").first()).toBeVisible();
  await qt().getByRole("button", { name: /Export/ }).click();
  await expect(qt().getByText("All rows (fetches the rest first)")).toBeVisible();
  await expect(qt().getByText("Only the 100 loaded rows")).toBeVisible();
  // "CSV" under All rows: fetches everything, then the file has every row
  const [dl] = await Promise.all([page.waitForEvent("download"), qt().locator(".ctx button", { hasText: /^CSV$/ }).first().click()]);
  const csv = readFileSync(await dl.path(), "utf-8").trim().split(/\r?\n/);
  expect(csv.length).toBe(12345 + 1); // header + rows
  await expect(qt().getByText("12,345 rows").first()).toBeVisible();
  await expect(qt().getByRole("button", { name: "Fetch all" })).toHaveCount(0); // cursor exhausted
});

test("Fetch all, and Rows = all", async () => {
  await run("1000");
  await expect(qt().getByText("1,000+ rows").first()).toBeVisible();
  await qt().getByRole("button", { name: "Fetch all" }).click();
  await expect(qt().getByText("12,345 rows").first()).toBeVisible();
  await run("0");
  await expect(qt().getByText("12,345 rows").first()).toBeVisible();
});
