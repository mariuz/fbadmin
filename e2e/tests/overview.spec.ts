import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { FB } from "./fixtures";

/**
 * Milestone 2.0 multi-server overview: a reachable server with a database, an unreachable
 * registration and one without a saved password; status filter, click-through to the dashboard,
 * and an axe audit in the light, dark and high-contrast themes.
 */
test.describe.configure({ mode: "serial" });

const OK = "E2E Overview A-OK";
const BAD = "E2E Overview down";
const NOPW = "E2E Overview no password";
const DB = "ovdb";
let page: Page;
const ids: string[] = [];
let dbId = "";

test.beforeAll(async ({ browser, request }) => {
  const add = async (data: Record<string, unknown>) => {
    const id = (await (await request.post("/api/servers", { data })).json()).id as string;
    ids.push(id);
    return id;
  };
  const ok = await add({ name: OK, host: FB.host, port: FB.port, user: FB.user, password: FB.password });
  const db = await request.post(`/api/servers/${ok}/databases/create`, {
    data: { name: DB, path: `${FB.dir}/fbadmin_e2eo_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  const bad = await add({ name: BAD, host: "127.0.0.1", port: 1, user: FB.user, password: "x" });
  await request.post(`/api/servers/${bad}/databases`, { data: { name: "lost", path: "/nowhere/lost.fdb" } });
  const nopw = await add({ name: NOPW, host: FB.host, port: FB.port, user: FB.user, save_password: false });
  await request.post(`/api/servers/${nopw}/databases`, { data: { name: "secret", path: "/nowhere/secret.fdb" } });
  page = await (await browser.newContext()).newPage();
  await page.goto("/");
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  if (ids[0] && dbId) await request.post(`/api/servers/${ids[0]}/databases/${dbId}/drop`);
  for (const id of ids) await request.delete(`/api/servers/${id}`);
});

const pane = () => page.locator(".tab-pane:not(.hidden)");
const card = (name: string) => pane().locator(".ov-server").filter({ has: page.locator("h3", { hasText: new RegExp(`^${name}$`) }) });

async function audit(what: string) {
  for (const theme of ["", "dark", "contrast"]) {
    await page.evaluate((th) => { if (th) document.documentElement.dataset.theme = th; else delete document.documentElement.dataset.theme; }, theme);
    const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
    expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), `${what} (${theme || "light"})`).toEqual([]);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
}

test("one page with every server and database", async () => {
  await page.locator(".topbar button", { hasText: "Overview" }).click();
  await expect(pane().locator(".ov")).toBeVisible();
  const ok = card(OK);
  await expect(ok.locator(".ov-head .ov-badge")).toHaveText(/OK/);
  await expect(ok.locator(".ov-head")).toContainText("Firebird");
  const row = ok.locator("tr", { hasText: DB });
  await expect(row.locator(".ov-badge")).toHaveText(/OK/);
  await expect(row).toContainText(/\d+(\.\d)? (KB|MB)/);

  const bad = card(BAD);
  await expect(bad.locator(".ov-head .ov-badge")).toHaveText(/Critical/);
  await expect(bad).toContainText("Unreachable");
  await expect(bad.locator("tr", { hasText: "lost" }).locator(".ov-badge")).toHaveText(/Critical/);

  const nopw = card(NOPW);
  await expect(nopw.locator(".ov-head .ov-badge")).toHaveText(/Unknown/);
  await expect(nopw).toContainText("Password not saved");
  // no password prompt was opened for it
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await audit("overview");
});

test("filter by status and text, sort", async () => {
  const filters = pane().getByRole("group", { name: "Filter by status" });
  await filters.getByRole("button", { name: /Critical/ }).click();
  await expect(card(BAD)).toBeVisible();
  await expect(card(OK)).toHaveCount(0);
  await expect(card(NOPW)).toHaveCount(0);
  await filters.getByRole("button", { name: /Critical/ }).click();
  await pane().getByLabel("Filter servers and databases").fill("overview password");
  await expect(card(NOPW)).toBeVisible();
  await expect(card(BAD)).toHaveCount(0);
  await pane().getByLabel("Filter servers and databases").fill("");
  // worst status first: the unreachable server comes before the healthy one
  const names = await pane().locator(".ov-server h3").allTextContents();
  expect(names.indexOf(BAD)).toBeLessThan(names.indexOf(OK));
  await pane().getByLabel("Sort").selectOption("name");
  await expect.poll(async () => {
    const sorted = await pane().locator(".ov-server h3").allTextContents();
    return sorted.indexOf(OK) < sorted.indexOf(BAD);
  }).toBe(true);
  await pane().locator("button", { hasText: "Check now" }).click();
  await expect(pane().locator(".toolbar")).toContainText("Checked at");
});

test("click through to the database dashboard", async () => {
  await card(OK).locator("tr", { hasText: DB }).locator("button", { hasText: "Dashboard" }).click();
  await expect(page.locator(".tab.active")).toHaveText("Dashboard");
  await expect(page.locator(".tab-pane:not(.hidden)")).toContainText(DB);
});
