import AxeBuilder from "@axe-core/playwright";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { FB, expand, treeRow } from "./fixtures";

/**
 * Roadmap 2.3: anonymised copies of production. Suggestions are applied, a rule on a key column reaches the
 * column that references it, the preview shows masked rows, and the copy is made and registered.
 * (backend/tests/test_anonymise.py checks the copy itself: masked values, foreign keys, triggers, leak check.)
 */
test.describe.configure({ mode: "serial" });

const SERVER = `E2E Anonymise ${Date.now().toString(36)}`;
const COPY = `${FB.dir}/fbadmin_e2eanoncopy_${Date.now().toString(36)}.fdb`;
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
    data: { name: "proddb", path: `${FB.dir}/fbadmin_e2eanon_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  await run(request, "CREATE TABLE CUSTOMERS (ID INTEGER NOT NULL PRIMARY KEY, EMAIL VARCHAR(60) NOT NULL UNIQUE, CUSTOMER_NAME VARCHAR(40));\n"
    + "CREATE TABLE ORDERS (ID INTEGER NOT NULL PRIMARY KEY, CUSTOMER_EMAIL VARCHAR(60) REFERENCES CUSTOMERS(EMAIL), AMOUNT INTEGER);");
  await run(request, "INSERT INTO CUSTOMERS VALUES (1, 'ann@corp.example', 'Ann Real');\nINSERT INTO CUSTOMERS VALUES (2, 'bob@corp.example', 'Bob Real');\n"
    + "INSERT INTO ORDERS VALUES (1, 'ann@corp.example', 10);\nINSERT INTO ORDERS VALUES (2, 'bob@corp.example', 20);");
  page = await (await browser.newContext()).newPage();
  page.on("dialog", (d) => d.accept());
  await page.goto("/");
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  const srv = await (await request.get(`/api/servers/${serverId}`)).json();
  for (const d of srv.databases ?? []) await request.post(`/api/servers/${serverId}/databases/${d.id}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

const pane = () => page.locator(".tab-pane:not(.hidden)");

async function audit(what: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
  expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), what).toEqual([]);
}

test("mask, preview and copy", async () => {
  await expand(page, SERVER);
  await treeRow(page, "proddb").click({ button: "right" });
  const ctx = page.locator(".ctx").first();
  const target = page.locator(".ctx button[role=menuitem]", { hasText: "Anonymised copy…" }).first();
  if (!(await target.isVisible())) {
    for (const sub of await ctx.locator(":scope > .ctx-sub > button").all()) {
      await sub.click();
      if (await target.isVisible()) break;
    }
  }
  await target.click();
  await pane().getByRole("button", { name: /^Apply 3 suggestion/ }).click();
  await expect(pane()).toContainText("3 column(s) masked");

  await pane().getByRole("button", { name: /CUSTOMERS/ }).click();
  await expect(pane().getByLabel("Masking of EMAIL")).toHaveValue("email");
  await expect(pane().getByLabel("Masking of CUSTOMER_NAME")).toHaveValue("name");
  await pane().getByRole("button", { name: "Preview masked rows" }).click();
  const prev = pane().getByRole("region", { name: "Preview of the masked rows" });
  await expect(prev).toContainText("@example.invalid");
  await expect(prev).not.toContainText("ann@corp.example");
  // keep the referencing column: the rule of the key still reaches it
  await pane().getByRole("button", { name: /ORDERS/ }).click();
  await pane().getByLabel("Masking of CUSTOMER_EMAIL").selectOption("keep");
  await expect(pane()).toContainText("masked through the foreign key from CUSTOMERS.EMAIL");
  await expect(pane()).toContainText("3 column(s) masked");

  await pane().getByLabel("Database file on the target server").fill(COPY);
  await pane().getByLabel("Register it as").fill("devdb");
  for (const theme of ["light", "dark", "contrast"]) {
    await page.evaluate((th) => { document.documentElement.dataset.theme = th; }, theme);
    await audit(`anonymised copy (${theme} theme)`);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });

  await pane().getByRole("button", { name: "Make the copy" }).click();
  const done = pane().locator(".alert.good");
  await expect(done).toContainText("No original value was found in the masked columns.", { timeout: 120_000 });
  await expect(done).toContainText("1 foreign key(s) checked");
  const srv = await (await page.request.get(`/api/servers/${serverId}`)).json();
  expect(srv.databases.map((d: { name: string }) => d.name)).toContain("devdb");
  await audit("anonymised copy (done)");
});
