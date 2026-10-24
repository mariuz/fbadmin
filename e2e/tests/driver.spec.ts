import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { expand, FB } from "./fixtures";

/**
 * The server-wide "Database driver" setting (user menu → Database driver…): automatic (the default),
 * native or pure Python; the driver in use and why, availability of the native driver, switching
 * drivers (pooled connections are closed when the driver in use changes), environment override, and
 * an axe audit of the panel. See docs/DRIVERS.md.
 */
test.describe.configure({ mode: "serial" });

const SERVER = "E2E Driver";
let serverId = "";
let dbId = "";

interface DriverStatus {
  driver: "auto" | "native" | "firebirdsql";
  source: string;
  effective: "native" | "firebirdsql";
  reason: string;
  fallback: string | null;
  native: { available: boolean; reason: string; client_version: string | null };
}

const status = async (request: import("@playwright/test").APIRequestContext): Promise<DriverStatus> =>
  (await request.get("/api/driver")).json();

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
  const st = await status(request);
  if (st.source !== "environment") await request.put("/api/driver", { data: { driver: "auto" } });
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

const radios = (panel: import("@playwright/test").Locator) => ({
  auto: panel.getByRole("radio", { name: /^Automatic \(recommended\)/ }),
  native: panel.getByRole("radio", { name: /^Native \(firebird-driver\)/ }),
  firebirdsql: panel.getByRole("radio", { name: /^Pure Python \(firebirdsql\)/ }),
});
const LABEL = { native: "Native (firebird-driver)", firebirdsql: "Pure Python (firebirdsql)" };

test("driver panel shows the setting, the driver in use and why, and passes axe", async ({ browser, request }) => {
  const page = await (await browser.newContext()).newPage();
  await page.goto("/");
  const st = await status(request);
  const panel = await openPanel(page);
  await expect(panel.getByRole("heading", { name: "Database driver" })).toBeVisible();
  const r = radios(panel);
  await expect(r[st.driver]).toBeChecked();
  if (st.source === "default") expect(st.driver).toBe("auto"); // the default setting
  const inUse = panel.locator("table.driver-effective");
  await expect(inUse).toContainText(LABEL[st.effective]);
  await expect(inUse).toContainText(st.reason);
  if (st.native.available) {
    await expect(panel.locator("table.kv").last()).toContainText(st.native.client_version ?? "");
  } else {
    await expect(r.native).toBeDisabled();
    await expect(panel).toContainText("Not available:");
    expect(st.effective).toBe("firebirdsql");
  }
  if (st.source === "environment") {
    await expect(r.firebirdsql).toBeDisabled();
    await expect(r.auto).toBeDisabled();
    await expect(panel).toContainText("FBADMIN_DRIVER");
  }
  await audit(page, "driver panel");
  await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
  await audit(page, "driver panel (dark theme)");
  await page.context().close();
});

test("switching drivers closes pooled connections; automatic comes back", async ({ browser, request }) => {
  const st = await status(request);
  test.skip(!st.native.available, `native driver unavailable: ${st.native.reason}`);
  test.skip(st.source === "environment", "driver set by FBADMIN_DRIVER");
  const page = await (await browser.newContext()).newPage();
  await page.goto("/");
  await expand(page, SERVER);
  await expand(page, "drv"); // opens a pooled metadata connection
  const panel = await openPanel(page);
  const r = radios(panel);
  const saveBtn = panel.getByRole("button", { name: "Save" });
  const exec = async () => {
    const res = await request.post(`/api/servers/${serverId}/databases/${dbId}/execute`, { data: { sql: "SELECT 1 FROM RDB$DATABASE" } });
    expect((await res.json()).error).toBeNull();
  };

  await r.firebirdsql.check();
  await saveBtn.click();
  await expect.poll(async () => (await status(request)).effective).toBe("firebirdsql");
  await exec(); // a pooled firebirdsql connection

  await r.native.check();
  await saveBtn.click();
  await expect(panel.getByRole("status").filter({ hasText: "pooled connection(s) closed" })).toBeVisible();
  await expect(panel.locator("table.driver-effective")).toContainText(LABEL.native);
  expect(await status(request)).toMatchObject({ driver: "native", effective: "native" });
  await exec(); // new connections use the native driver

  await r.auto.check();
  await saveBtn.click();
  await expect.poll(async () => (await status(request)).driver).toBe("auto");
  const after = await status(request);
  if (st.driver === "auto") expect(after.effective).toBe(st.effective); // resolves the same way again
  await expect(panel.locator("table.driver-effective")).toContainText(after.reason);
  await exec();
  await page.context().close();
});
