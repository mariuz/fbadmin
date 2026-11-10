import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { FB, expand, treeRow } from "./fixtures";

/**
 * Roadmap 2.4: change requests. Propose a SQL change, run the dry run (the schema diff on a copy), approve it and
 * apply it; the history records each step. (backend/tests/test_changes.py covers server mode, edits, a schema
 * changed since the dry run and migration scripts.)
 */
test.describe.configure({ mode: "serial" });

const SERVER = `E2E Changes ${Date.now().toString(36)}`;
let page: Page;
let serverId = "";
let dbId = "";
const base = () => `/api/servers/${serverId}/databases/${dbId}`;

test.beforeAll(async ({ browser, request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, {
    data: { name: "reviewdb", path: `${FB.dir}/fbadmin_e2ecr_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  const r = await (await request.post(base() + "/execute", { data: { sql: "CREATE TABLE ORDERS (ID INTEGER NOT NULL PRIMARY KEY, NOTE VARCHAR(20))" } })).json();
  expect(r.error).toBeNull();
  page = await (await browser.newContext()).newPage();
  await page.goto("/");
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  await request.post(`${base()}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

const pane = () => page.locator(".tab-pane:not(.hidden)");

async function audit(what: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
  expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), what).toEqual([]);
}

async function themes(what: string) {
  for (const theme of ["light", "dark", "contrast"]) {
    await page.evaluate((th) => { document.documentElement.dataset.theme = th; }, theme);
    await audit(`${what} (${theme} theme)`);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
}

test("propose, dry run, approve and apply a change", async () => {
  test.setTimeout(120_000);
  await expand(page, SERVER);
  await treeRow(page, "reviewdb").click({ button: "right" });
  const ctx = page.locator(".ctx").first();
  const target = page.locator(".ctx button[role=menuitem]", { hasText: "Change requests…" }).first();
  if (!(await target.isVisible())) {
    for (const sub of await ctx.locator(":scope > .ctx-sub > button").all()) {
      await sub.click();
      if (await target.isVisible()) break;
    }
  }
  await target.click();
  await expect(pane().getByText("No open change requests.")).toBeVisible();
  await pane().getByRole("button", { name: "New change request" }).click();
  await pane().getByLabel("Title").fill("Track shipping");
  await pane().getByLabel("Description").fill("For the courier report");
  await pane().locator(".cr-editor .cm-content").click();
  await page.keyboard.type("ALTER TABLE ORDERS ADD SHIPPED TIMESTAMP;");
  await themes("new change request");
  await pane().getByRole("button", { name: "Propose the change" }).click();

  const list = pane().getByRole("list", { name: "Change requests" });
  await expect(list).toContainText("Track shipping");
  await expect(pane().getByText("Run the dry run first: it shows what the change does to the schema")).toBeVisible();
  await pane().getByRole("button", { name: "Run the dry run", exact: true }).click();
  const changes = pane().getByRole("list", { name: "Schema changes" });
  await expect(changes).toContainText("ORDERS", { timeout: 60_000 });
  await expect(changes).toContainText("altered");
  await expect(changes).toContainText("SHIPPED");
  await themes("change request with its dry run");

  await pane().getByLabel("Review comment").fill("Looks right");
  await pane().getByRole("button", { name: "Approve", exact: true }).click();
  await expect(pane().getByText("Approvals: 1 of 1")).toBeVisible();
  page.once("dialog", (d) => d.accept());
  await pane().getByRole("button", { name: "Apply the change" }).click();
  await expect(pane().getByText(/^Applied by /)).toBeVisible({ timeout: 60_000 });
  const history = pane().locator(".cr-history");
  for (const step of ["proposed the change", "ran the dry run", "approved", "applied the change"]) await expect(history).toContainText(step);
  await expect(history).toContainText("Looks right");
  await expect(pane().getByText("No open change requests.")).toBeVisible(); // applied: no longer open
  await pane().getByLabel("Show").selectOption("");
  await expect(list).toContainText("applied");
});

test("the column exists and the timeline has the change", async ({ request }) => {
  const r = await (await request.post(base() + "/execute", { data: { sql: "SELECT SHIPPED FROM ORDERS" } })).json();
  expect(r.error).toBeNull();
  const tl = (await (await request.get(base() + "/timeline")).json()).events as { action: string }[];
  expect(tl.map((e) => e.action)).toContain("change_applied");
});
