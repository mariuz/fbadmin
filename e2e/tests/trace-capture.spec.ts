import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { FB, expand, treeRow } from "./fixtures";

/**
 * Roadmap 2.4: continuous trace capture. In the History view, the continuous trace needs the history; turned on,
 * its session runs and records finished statements. (backend/tests/test_trace_capture.py covers regressions,
 * restarts and the backoff.)
 */
test.describe.configure({ mode: "serial" });

const SERVER = `E2E Trace capture ${Date.now().toString(36)}`;
let page: Page;
let serverId = "";
let dbId = "";
const base = () => `/api/servers/${serverId}/databases/${dbId}`;

test.beforeAll(async ({ browser, request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, {
    data: { name: "tracedb", path: `${FB.dir}/fbadmin_e2etc_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  page = await (await browser.newContext()).newPage();
  await page.goto("/");
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  await request.put(base() + "/monitoring/trace-capture", { data: { enabled: false } });
  await request.put(base() + "/monitoring/history/tracking", { data: { enabled: false } });
  await request.delete(base() + "/monitoring/history");
  await request.post(`${base()}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

const pane = () => page.locator(".tab-pane:not(.hidden)");

async function audit(what: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
  expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), what).toEqual([]);
}

test("turn the continuous trace on and see it record", async ({ request }) => {
  test.setTimeout(90_000);
  await expand(page, SERVER);
  await treeRow(page, "tracedb").click({ button: "right" });
  const ctx = page.locator(".ctx").first();
  const target = page.locator(".ctx button[role=menuitem]", { hasText: "Performance dashboard" }).first();
  if (!(await target.isVisible())) {
    for (const sub of await ctx.locator(":scope > .ctx-sub > button").all()) {
      await sub.click();
      if (await target.isVisible()) break;
    }
  }
  await target.click();
  await pane().locator(".subtabs").getByRole("button", { name: "History", exact: true }).click();
  const group = pane().getByRole("group", { name: "Continuous trace" });
  const toggle = group.getByLabel("Continuous trace");
  await expect(toggle).toBeDisabled();
  await expect(group).toContainText("Record the history of this database to turn it on.");
  const history = pane().getByLabel("Record history for this database");
  await history.click();
  await expect(history).toBeChecked();
  await expect(toggle).toBeEnabled();
  await group.getByLabel("Record statements slower than").fill("1");
  await toggle.click();
  await expect(page.getByText("Continuous trace started")).toBeVisible({ timeout: 20_000 });
  await expect(group.locator(".pill")).toHaveText("running");

  const sql = "SELECT COUNT(*) FROM RDB$FIELDS a CROSS JOIN RDB$FIELDS b";
  await expect.poll(async () => {
    await request.post(base() + "/execute", { data: { sql } });
    return (await (await request.get(base() + "/monitoring/trace-capture")).json()).captured;
  }, { timeout: 30_000 }).toBeGreaterThan(0);
  await page.reload();
  await expand(page, SERVER);
  await treeRow(page, "tracedb").click({ button: "right" });
  if (!(await target.isVisible())) {
    for (const sub of await page.locator(".ctx").first().locator(":scope > .ctx-sub > button").all()) {
      await sub.click();
      if (await target.isVisible()) break;
    }
  }
  await target.click();
  await pane().locator(".subtabs").getByRole("button", { name: "History", exact: true }).click();
  await expect(group).toContainText(/statement\(s\) recorded/);
  for (const theme of ["light", "dark", "contrast"]) {
    await page.evaluate((th) => { document.documentElement.dataset.theme = th; }, theme);
    await audit(`continuous trace (${theme} theme)`);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });

  await toggle.click();
  await expect(page.getByText("Continuous trace turned off")).toBeVisible();
  expect((await (await request.get(base() + "/monitoring/trace-capture")).json()).state).toBe("stopped");
});
