import AxeBuilder from "@axe-core/playwright";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { FB, expand, setEditor, treeRow } from "./fixtures";

/**
 * Roadmap 2.3: SQL notebooks. A notebook is created from a database's context menu; a SQL cell runs through a
 * Query Tool session, its result is charted, a note is written in Markdown, the notebook saves itself, and it
 * comes back with its results and chart from the Notebooks list. Exports are offered as Markdown and JSON.
 * (backend/tests/test_notebooks.py covers conflicts, revisions, import and sharing with permissions.)
 */
test.describe.configure({ mode: "serial" });

const SERVER = `E2E Notebooks ${Date.now().toString(36)}`;
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
    data: { name: "salesdb", path: `${FB.dir}/fbadmin_e2enb_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  await run(request, "CREATE TABLE SALES (REGION VARCHAR(20), AMOUNT INTEGER, ITEMS INTEGER);");
  await run(request, "INSERT INTO SALES VALUES ('north', 120, 4);\nINSERT INTO SALES VALUES ('south', 80, 2);\n"
    + "INSERT INTO SALES VALUES ('east', 45, 3);\nINSERT INTO SALES VALUES ('north', 30, 1);");
  page = await (await browser.newContext()).newPage();
  await page.goto("/");
});

test.afterAll(async ({ request }) => {
  for (const n of (await (await request.get("/api/notebooks", { params: { database_id: dbId } })).json()).notebooks ?? []) {
    await request.delete(`/api/notebooks/${n.id}`);
  }
  await page?.context().close();
  await request.post(`/api/servers/${serverId}/databases/${dbId}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

const pane = () => page.locator(".tab-pane:not(.hidden)");

async function audit(what: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
  expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), what).toEqual([]);
}

test("write, run, chart, note and reopen a notebook", async () => {
  await expand(page, SERVER);
  await treeRow(page, "salesdb").click({ button: "right" });
  const ctx = page.locator(".ctx").first();
  const target = page.locator(".ctx button[role=menuitem]", { hasText: "New notebook…" }).first();
  if (!(await target.isVisible())) {
    for (const sub of await ctx.locator(":scope > .ctx-sub > button").all()) {
      await sub.click();
      if (await target.isVisible()) break;
    }
  }
  await target.click();
  await expect(pane().getByLabel("Notebook name")).toHaveValue("Untitled notebook");
  await pane().getByLabel("Notebook name").fill("Sales by region");
  await expect(pane().getByLabel("Database")).toHaveValue(`${serverId}/${dbId}`);

  const first = pane().getByRole("region", { name: "Cell 1: SQL" });
  await setEditor(page, first, "SELECT REGION, SUM(AMOUNT) AS TOTAL, SUM(ITEMS) AS ITEMS FROM SALES GROUP BY REGION ORDER BY 2 DESC");
  await page.keyboard.press("ControlOrMeta+Enter");
  const grid = first.getByRole("region", { name: "Result 1" });
  await expect(grid).toContainText("north", { timeout: 15_000 });
  await expect(grid).toContainText("150");
  await expect(first).toContainText("3 row(s) fetched");

  // a horizontal bar chart of two value columns (the Query Tool's chart view, saved with the cell)
  await first.getByRole("button", { name: "Chart the result" }).click();
  const box = first.locator(".nb-chartbox");
  await box.locator(".qchart-controls select").first().selectOption("hbar");
  await expect(box.locator(".qchart-controls select").nth(1)).toHaveValue("0");
  await box.locator("details.ys > summary").click();
  await box.getByRole("checkbox", { name: "TOTAL" }).check();
  await box.getByRole("checkbox", { name: "ITEMS" }).check();
  await box.locator("details.ys > summary").click();
  const chart = box.getByRole("img", { name: "Horizontal bar chart of TOTAL, ITEMS by REGION" });
  await expect(chart).toBeVisible();
  await chart.focus();
  await page.keyboard.press("ArrowRight");
  await expect(box.locator(".qchart-tip")).toContainText("north");

  // a note above the query
  await pane().getByRole("button", { name: "+ Note" }).last().click();
  const note = pane().getByRole("region", { name: "Cell 2: Note" });
  await note.getByLabel("Note (Markdown)").fill("## Findings\n- **north** sells the most");
  await note.getByRole("button", { name: "Done" }).click();
  await expect(note.getByRole("heading", { name: "Findings" })).toBeVisible();
  await note.getByRole("button", { name: "Move up" }).click();
  await expect(pane().getByRole("region", { name: "Cell 1: Note" })).toBeVisible();
  await expect(pane().getByRole("status").filter({ hasText: /^Saved$/ })).toBeVisible({ timeout: 10_000 });

  for (const theme of ["light", "dark", "contrast"]) {
    await page.evaluate((th) => { document.documentElement.dataset.theme = th; }, theme);
    await audit(`notebook (${theme} theme)`);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });

  const md = pane().getByRole("link", { name: "Export Markdown" });
  await expect(md).toHaveAttribute("href", /\/api\/notebooks\/[0-9a-f]+\/export\?format=md/);
  const text = await (await page.request.get((await md.getAttribute("href"))!)).text();
  expect(text).toContain("# Sales by region");
  expect(text).toContain("| REGION | TOTAL | ITEMS |");
  expect(text).toContain("*Chart: hbar of TOTAL, ITEMS by REGION*");

  // after a reload, reopen it from the Notebooks list: results and chart were saved
  await page.reload();
  await page.locator(".topbar button", { hasText: "Notebooks" }).click();
  const list = pane().getByRole("region", { name: "Notebooks" });
  await expect(list).toContainText("Sales by region");
  await audit("notebook list");
  await list.locator("tr", { hasText: "Sales by region" }).click();
  const q = pane().getByRole("region", { name: "Cell 2: SQL" });
  await expect(q.getByRole("region", { name: "Result 1" })).toContainText("north");
  await expect(q.getByRole("img", { name: "Horizontal bar chart of TOTAL, ITEMS by REGION" })).toBeVisible();
  await expect(pane().getByRole("region", { name: "Cell 1: Note" }).getByRole("heading", { name: "Findings" })).toBeVisible();
});
