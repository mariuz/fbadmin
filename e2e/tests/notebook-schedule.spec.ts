import AxeBuilder from "@axe-core/playwright";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { createServer, type Server } from "node:http";
import { FB, expand, treeRow } from "./fixtures";

/**
 * Roadmap 2.4: scheduled notebooks. A notebook is scheduled from its Schedule… dialog with a webhook channel,
 * run now, and the report (summary, first rows, the notebook as Markdown) arrives; the Schedules tab shows it.
 * (backend/tests/test_notebook_runs.py covers the send policy, read-only runs, errors, e-mail attachments and
 * the owner's permissions.)
 */
test.describe.configure({ mode: "serial" });

const SERVER = `E2E NbSched ${Date.now().toString(36)}`;
let page: Page;
let serverId = "";
let dbId = "";
let nbId = "";
let channelId = "";
let hook: Server;
const received: any[] = [];

async function run(request: APIRequestContext, sql: string) {
  const s = await (await request.post("/api/query/sessions", { data: { server_id: serverId, database_id: dbId } })).json();
  const r = await (await request.post(`/api/query/sessions/${s.session_id}/execute`, { data: { sql, autocommit: true } })).json();
  expect(r.error, JSON.stringify(r.error)).toBeNull();
  await request.delete(`/api/query/sessions/${s.session_id}`);
}

test.beforeAll(async ({ browser, request }) => {
  hook = createServer((req, res) => {
    let data = "";
    req.on("data", (c) => (data += c));
    req.on("end", () => { received.push({ headers: req.headers, body: JSON.parse(data || "{}") }); res.end("ok"); });
  });
  await new Promise<void>((r) => hook.listen(0, "127.0.0.1", () => r()));
  const url = `http://127.0.0.1:${(hook.address() as { port: number }).port}/report`;
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, {
    data: { name: "reportdb", path: `${FB.dir}/fbadmin_e2enbs_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  await run(request, "CREATE TABLE ORDERS (ID INTEGER NOT NULL PRIMARY KEY, STATUS VARCHAR(10));");
  await run(request, "INSERT INTO ORDERS VALUES (1, 'stuck');\nINSERT INTO ORDERS VALUES (2, 'open');");
  const ch = await request.post("/api/alerts/channels", { data: { name: "team hook", kind: "webhook", settings: { url } } });
  expect(ch.status(), await ch.text()).toBe(201);
  channelId = (await ch.json()).id;
  nbId = (await (await request.post("/api/notebooks", { data: { name: "Stuck orders", server_id: serverId, database_id: dbId, cells: [
    { id: "n1", type: "markdown", source: "## Orders stuck since yesterday" },
    { id: "q1", type: "sql", source: "SELECT ID, STATUS FROM ORDERS WHERE STATUS = 'stuck'" },
  ] } })).json()).id;
  page = await (await browser.newContext()).newPage();
  page.on("dialog", (d) => d.accept());
  await page.goto("/");
});

test.afterAll(async ({ request }) => {
  for (const s of await (await request.get("/api/schedules")).json()) if (s.database_id === dbId) await request.delete(`/api/schedules/${s.id}`);
  await request.delete(`/api/alerts/channels/${channelId}`);
  await request.delete(`/api/notebooks/${nbId}`);
  await page?.context().close();
  hook?.close();
  await request.post(`/api/servers/${serverId}/databases/${dbId}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

const pane = () => page.locator(".tab-pane:not(.hidden)");

async function audit(what: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
  expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), what).toEqual([]);
}

test("schedule a notebook, run it, receive the report", async () => {
  await expand(page, SERVER);
  await treeRow(page, "reportdb").click({ button: "right" });
  const ctx = page.locator(".ctx").first();
  const target = page.locator(".ctx button[role=menuitem]", { hasText: "Notebooks…" }).first();
  if (!(await target.isVisible())) {
    for (const sub of await ctx.locator(":scope > .ctx-sub > button").all()) {
      await sub.click();
      if (await target.isVisible()) break;
    }
  }
  await target.click();
  await pane().getByRole("region", { name: "Notebooks" }).locator("tr", { hasText: "Stuck orders" }).click();
  await pane().getByRole("button", { name: "⏱ Schedule…" }).click();

  const dlg = page.getByRole("dialog");
  await expect(dlg.getByLabel("Name")).toHaveValue("Stuck orders report");
  await dlg.getByLabel("When (cron)").fill("0 7 * * 1-5");
  await dlg.getByRole("checkbox", { name: /team hook/ }).check();
  await dlg.getByLabel("Send it").selectOption("rows");
  await expect(dlg.getByRole("checkbox", { name: "Read only (refuse cells that change data)" })).toBeChecked();
  for (const theme of ["light", "dark", "contrast"]) {
    await page.evaluate((th) => { document.documentElement.dataset.theme = th; }, theme);
    await audit(`schedule dialog (${theme} theme)`);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
  await dlg.getByRole("button", { name: "Schedule it" }).click();
  await expect(page.locator(".toast").last()).toContainText("Scheduled");
  const mine = dlg.getByRole("region", { name: "Schedules of this notebook" });
  await expect(mine).toContainText("Stuck orders report");

  await mine.getByRole("button", { name: "Run now" }).click();
  await expect(page.locator(".toast").last()).toContainText("Run finished", { timeout: 60_000 });
  await expect.poll(() => received.length, { timeout: 15_000 }).toBeGreaterThan(0);
  const r = received[received.length - 1];
  expect(r.headers["x-fbadmin-event"]).toBe("notebook.report");
  expect(r.body.title).toBe("Notebook report: Stuck orders");
  expect(r.body.message).toContain("1 | stuck");
  expect(r.body.details.markdown).toContain("## Orders stuck since yesterday");
  expect(r.body.details.markdown).toContain("| 1 | stuck |");
  await page.keyboard.press("Escape");

  // the Schedules tab lists it with its result
  await page.locator(".topbar button", { hasText: "Schedules" }).click();
  const row = pane().locator("tr", { hasText: "Stuck orders report" }).first();  // the schedule (its run is in the history below)
  await expect(row).toContainText("notebook");
  await row.click();
  await expect(pane()).toContainText("1 cell(s), 1 row(s) · sent to team hook");
  await row.getByRole("button", { name: "Edit" }).click();
  await expect(pane().getByLabel("Notebook", { exact: true })).toHaveValue(nbId);
  await expect(pane().getByLabel("Send it")).toHaveValue("rows");
  await audit("schedules tab with a notebook schedule");
});
