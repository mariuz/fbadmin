import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { FB } from "./fixtures";

/**
 * Roadmap 2.5: weekly digest per database. A "Digest" schedule with its period and channels; Run now records the
 * digest and the database's status in the history. (backend/tests/test_digest.py covers its content and delivery.)
 * The digest connects to no database, so a registered path that does not exist is enough (the server is real:
 * the schedule form reads its backup options).
 */
test.describe.configure({ mode: "serial" });

const SERVER = `E2E Digest ${Date.now().toString(36)}`;
let page: Page;
let serverId = "";
let scheduleId = "";

test.beforeAll(async ({ browser, request }) => {
  serverId = (await (await request.post("/api/servers", { data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password } })).json()).id;
  expect((await request.post(`/api/servers/${serverId}/databases`, { data: { name: "shop", path: "/nowhere/shop.fdb" } })).status()).toBe(201);
  page = await (await browser.newContext()).newPage();
  await page.goto("/");
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  if (scheduleId) await request.delete(`/api/schedules/${scheduleId}`);
  await request.delete(`/api/servers/${serverId}`);
});

const pane = () => page.locator(".tab-pane:not(.hidden)");

test("a digest schedule", async () => {
  await page.locator(".topbar button", { hasText: "Schedules" }).click();
  await pane().locator("button", { hasText: "New schedule" }).click();
  await pane().getByLabel("Name", { exact: true }).fill("Weekly shop digest");
  await pane().locator("select", { has: page.locator('option[value="digest"]') }).selectOption("digest");
  await pane().locator("select", { has: page.locator(`option:text-is("${SERVER}")`) }).first().selectOption({ label: SERVER });
  await pane().getByLabel("Database", { exact: true }).selectOption({ label: "shop" });
  await pane().getByLabel("Period").selectOption("14");
  await expect(pane().getByRole("group", { name: "Send the digest to" })).toBeVisible();
  for (const theme of ["light", "dark", "contrast"]) {
    await page.evaluate((th) => { document.documentElement.dataset.theme = th; }, theme);
    const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
    expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), theme).toEqual([]);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
  const [resp] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith("/api/schedules") && r.request().method() === "POST"),
    pane().locator("button.btn.primary", { hasText: "Save" }).click(),
  ]);
  expect(resp.status(), await resp.text()).toBe(201);
  const s = await resp.json();
  scheduleId = s.id;
  expect(s.digest).toEqual({ channels: [], days: 14 });
  const row = pane().locator("table.grid").first().locator("tbody tr", { hasText: "Weekly shop digest" });
  await row.getByTitle("Run now").click();
  const history = pane().getByRole("region", { name: /History/ });
  await expect(history).toContainText("database status: unknown", { timeout: 20_000 });
});
