import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { expand, FB } from "./fixtures";

/**
 * The server-wide "Database driver" setting (user menu → Database driver…): availability of the
 * native driver, switching drivers (pooled connections are closed), environment override, and an
 * axe audit of the panel. See docs/DRIVERS.md.
 */
test.describe.configure({ mode: "serial" });

const SERVER = "E2E Driver";
let serverId = "";
let dbId = "";

interface DriverStatus {
  driver: string;
  source: string;
  native: { available: boolean; reason: string; client_version: string | null };
}

test.beforeAll(async ({ request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, {
    data: { name: "drv", path: `${FB.dir}/fbadmin_e2edrv_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
});

test.afterAll(async ({ request }) => {
  const st: DriverStatus = await (await request.get("/api/driver")).json();
  if (st.source !== "environment") await request.put("/api/driver", { data: { driver: "firebirdsql" } });
  if (!serverId) return;
  await request.post(`/api/servers/${serverId}/databases/${dbId}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

async function audit(page: Page, what: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
  const summary = r.violations.map((v) => `${v.id} (${v.impact}): ${v.nodes.length}× ${v.nodes.slice(0, 3).map((n) => n.target.join(" ")).join(" | ")}`);
  expect(summary, `${what}: axe violations`).toEqual([]);
}

async function openPanel(page: Page) {
  await page.locator(".user-menu > button.tb").click();
  await page.locator(".user-menu-pop button", { hasText: "Database driver…" }).click();
  const panel = page.locator(".driver-settings");
  await expect(panel).toBeVisible();
  return panel;
}

test("driver panel shows availability and passes axe", async ({ browser, request }) => {
  const page = await (await browser.newContext()).newPage();
  await page.goto("/");
  const st: DriverStatus = await (await request.get("/api/driver")).json();
  const panel = await openPanel(page);
  await expect(panel.getByRole("heading", { name: "Database driver" })).toBeVisible();
  const pure = panel.getByRole("radio", { name: /firebirdsql/ });
  const native = panel.getByRole("radio", { name: /^native/ });
  await expect(st.driver === "native" ? native : pure).toBeChecked();
  if (st.native.available) {
    await expect(panel.locator("table.kv")).toContainText(st.native.client_version ?? "");
  } else {
    await expect(native).toBeDisabled();
    await expect(panel).toContainText("Not available:");
  }
  if (st.source === "environment") {
    await expect(pure).toBeDisabled();
    await expect(panel).toContainText("FBADMIN_DRIVER");
  }
  await audit(page, "driver panel");
  await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
  await audit(page, "driver panel (dark theme)");
  await page.context().close();
});

test("switching to the native driver closes pooled connections", async ({ browser, request }) => {
  const st: DriverStatus = await (await request.get("/api/driver")).json();
  test.skip(!st.native.available, `native driver unavailable: ${st.native.reason}`);
  test.skip(st.source === "environment", "driver set by FBADMIN_DRIVER");
  const page = await (await browser.newContext()).newPage();
  await page.goto("/");
  await expand(page, SERVER);
  await expand(page, "drv"); // opens a pooled metadata connection
  const panel = await openPanel(page);
  await panel.getByRole("radio", { name: /^native/ }).check();
  await panel.getByRole("button", { name: "Save" }).click();
  await expect(panel.getByRole("status").filter({ hasText: "pooled connection(s) closed" })).toBeVisible();
  await expect(panel.locator("table.kv")).toContainText("native");
  expect((await (await request.get("/api/driver")).json()).driver).toBe("native");
  // new connections use the native driver
  const r = await request.post(`/api/servers/${serverId}/databases/${dbId}/execute`, { data: { sql: "SELECT 1 FROM RDB$DATABASE" } });
  expect((await r.json()).error).toBeNull();
  await panel.getByRole("radio", { name: /firebirdsql/ }).check();
  await panel.getByRole("button", { name: "Save" }).click();
  await expect.poll(async () => (await (await request.get("/api/driver")).json()).driver).toBe("firebirdsql");
  await page.context().close();
});
