import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { expand, FB, treeRow } from "./fixtures";

/**
 * Milestone 1.2 event monitor: opened from the database context menu, names suggested from PSQL
 * sources, events posted (from the UI and by a trigger) arrive live with counts.
 *
 * Events need a server whose RemoteAuxPort is reachable: FB_EVENTS_PORT (e.g. 3060 from
 * scripts/events-test-server.sh). The plain FB_PORT server is used to check the error shown when
 * the auxiliary port cannot be reached (Docker publishes only 3050 there).
 */
test.describe.configure({ mode: "serial" });

const EV_PORT = Number(process.env.FB_EVENTS_PORT ?? 0);
const SERVER = "E2E Events";
const PLAIN = "E2E Events plain";
let page: Page;
const created: Array<{ sid: string; dbid: string }> = [];

const pane = () => page.locator(".tab-pane:not(.hidden)");

async function audit(what: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
  expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), what).toEqual([]);
}

async function registerServer(request: import("@playwright/test").APIRequestContext, name: string, port: number, label: string, sql?: string) {
  const sid = (await (await request.post("/api/servers", {
    data: { name, host: FB.host, port, user: FB.user, password: FB.password },
  })).json()).id;
  const path = `${FB.dir}/fbadmin_e2eev_${Date.now().toString(36)}.fdb`;
  const db = await request.post(`/api/servers/${sid}/databases/create`, { data: { name: label, path } });
  expect(db.status(), await db.text()).toBe(201);
  const dbid = (await db.json()).id;
  created.push({ sid, dbid });
  if (sql) {
    const r = await (await request.post(`/api/servers/${sid}/databases/${dbid}/execute`, { data: { sql } })).json();
    expect(r.error, JSON.stringify(r.error)).toBeNull();
  }
  return { sid, dbid };
}

async function openMonitor(server: string, label: string) {
  await expand(page, server);
  await treeRow(page, label).click({ button: "right" });
  const ctx = page.locator(".ctx").first();
  await expect(ctx).toBeVisible();
  const target = page.locator(".ctx button[role=menuitem]", { hasText: "Event monitor" }).first();
  if (!(await target.isVisible())) {
    for (const sub of await ctx.locator(":scope > .ctx-sub > button").all()) {
      await sub.click();
      if (await target.isVisible()) break;
    }
  }
  await target.click();
}

test.beforeAll(async ({ browser }) => {
  page = await (await browser.newContext()).newPage();
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  for (const { sid, dbid } of created) {
    await request.post(`/api/servers/${sid}/databases/${dbid}/drop`);
    await request.delete(`/api/servers/${sid}`);
  }
});

test("event monitor: register names, post events, watch counts arrive live", async ({ request }) => {
  test.skip(!EV_PORT, "FB_EVENTS_PORT not set (start a server with scripts/events-test-server.sh)");
  const { sid, dbid } = await registerServer(request, SERVER, EV_PORT, "evdb", `
CREATE TABLE ORDERS (ID INTEGER);
SET TERM ^ ;
CREATE TRIGGER ORDERS_AI FOR ORDERS AFTER INSERT AS BEGIN POST_EVENT 'ORDER_NEW'; END^
CREATE PROCEDURE PAY AS BEGIN POST_EVENT 'ORDER_PAID'; END^
SET TERM ; ^
`);
  await page.goto("/");
  await openMonitor(SERVER, "evdb");
  await expect(pane()).toContainText("Event monitor");
  await expect(pane().locator(".pill", { hasText: "listening" })).toBeVisible();

  // suggestions from PSQL sources
  const sugg = pane().locator(".ev-sugg");
  await expect(sugg).toContainText("ORDER_NEW");
  await expect(sugg).toContainText("trigger ORDERS_AI (ORDERS)");
  await expect(sugg).toContainText("procedure PAY");
  await pane().getByRole("button", { name: "Register ORDER_NEW" }).click();
  // and typed names
  await pane().getByLabel("Event names to register").fill("MANUAL_ONE, MANUAL_TWO");
  await pane().getByRole("button", { name: "Register", exact: true }).click();
  const names = pane().locator("table.ev-names");
  await expect(names).toContainText("MANUAL_TWO");
  await expect(names).not.toContainText("registering…");

  // post from the UI, three times
  await pane().getByLabel("Event name to post").fill("MANUAL_ONE");
  await pane().getByLabel("times").fill("3");
  await pane().getByRole("button", { name: "Post", exact: true }).click();
  await expect(pane().getByTestId("count-MANUAL_ONE")).toHaveText("3");
  // a trigger posts after its transaction commits
  const r = await (await request.post(`/api/servers/${sid}/databases/${dbid}/execute`, {
    data: { sql: "INSERT INTO ORDERS VALUES (1); INSERT INTO ORDERS VALUES (2); COMMIT;" },
  })).json();
  expect(r.error, JSON.stringify(r.error)).toBeNull();
  await expect(pane().getByTestId("count-ORDER_NEW")).toHaveText("2");
  await expect(pane().getByTestId("count-MANUAL_TWO")).toHaveText("0");
  const log = pane().locator("table.ev-log tbody");
  await expect(log).toContainText("MANUAL_ONE");
  await expect(log).toContainText("ORDER_NEW");
  await audit("event monitor");

  // unregister one name, reset counters
  await pane().getByRole("button", { name: "Unregister MANUAL_TWO" }).click();
  await expect(names).not.toContainText("MANUAL_TWO");
  await pane().getByRole("button", { name: "Reset counters" }).click();
  await expect(pane().getByTestId("count-MANUAL_ONE")).toHaveText("0");
  await expect(log.locator("tr")).toHaveCount(0);
});

test("event monitor: explains an unreachable RemoteAuxPort", async ({ request }) => {
  await registerServer(request, PLAIN, FB.port, "evplain");
  await page.goto("/");
  await openMonitor(PLAIN, "evplain");
  const alert = pane().locator(".ev-openerr");
  const listening = pane().locator(".pill", { hasText: "listening" });
  await expect(alert.or(listening)).toBeVisible({ timeout: 30_000 });
  test.skip(await listening.isVisible(), "the auxiliary port of the FB_PORT server is reachable");
  await expect(alert).toContainText("Cannot open the event connection");
  await expect(alert).toContainText("RemoteAuxPort");
  await expect(alert.getByRole("button", { name: "Try again" })).toBeVisible();
  await audit("event monitor error");
});
