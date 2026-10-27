import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { FB } from "./fixtures";

/**
 * Roadmap 2.2: backup encryption and restore drills. A backup key is created (and shown once), an encrypted
 * backup schedule runs, and a restore drill restores its newest backup and reports its checks.
 */
test.describe.configure({ mode: "serial" });

const SERVER = `E2E Drill ${Date.now().toString(36)}`;
let page: Page;
let serverId = "";
let dbId = "";
const schedules: string[] = [];
let keyId = "";

test.beforeAll(async ({ request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  const path = `${FB.dir}/fbadmin_e2edrill_${Date.now().toString(36)}.fdb`;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, { data: { name: "shop", path } });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  const sql = "CREATE TABLE ORDERS (ID INTEGER NOT NULL PRIMARY KEY);\nINSERT INTO ORDERS VALUES (1);\nINSERT INTO ORDERS VALUES (2);\n";
  const r = await (await request.post(`/api/servers/${serverId}/databases/${dbId}/execute`, { data: { sql } })).json();
  expect(r.error, JSON.stringify(r.error)).toBeNull();
});

test.beforeAll(async ({ browser }) => {
  page = await (await browser.newContext()).newPage();
  await page.goto("/");
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  for (const id of schedules) await request.delete(`/api/schedules/${id}?delete_backups=true`);
  if (keyId) await request.delete(`/api/backup-keys/${keyId}?force=true`);
  if (serverId) {
    await request.post(`/api/servers/${serverId}/databases/${dbId}/drop`);
    await request.delete(`/api/servers/${serverId}`);
  }
});

const pane = () => page.locator(".tab-pane:not(.hidden)");

async function audit(what: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
  expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), what).toEqual([]);
}

async function newSchedule(name: string, kind: string) {
  await pane().locator("button", { hasText: "New schedule" }).click();
  await pane().getByLabel("Name", { exact: true }).fill(name);
  await pane().locator("select", { has: page.locator(`option[value="${kind}"]`) }).selectOption(kind);
  await pane().locator("select", { has: page.locator(`option:text-is("${SERVER}")`) }).first().selectOption({ label: SERVER });
  await pane().getByLabel("Database", { exact: true }).selectOption({ label: "shop" });
}

async function saveAndRun(name: string) {
  const [resp] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith("/api/schedules") && r.request().method() === "POST"),
    pane().locator("button.btn.primary", { hasText: "Save" }).click(),
  ]);
  expect(resp.status(), await resp.text()).toBe(201);
  schedules.push((await resp.json()).id);
  const row = pane().locator("table.grid").first().locator("tbody tr", { hasText: name });
  await row.getByTitle("Run now").click();
  return row;
}

test("backup key, encrypted backup schedule", async () => {
  await page.locator(".topbar button", { hasText: "Schedules" }).click();
  const keys = pane().locator(".mt-section", { hasText: "Backup encryption" });
  await keys.getByRole("button", { name: "Create a backup key" }).click();
  const shown = keys.locator(".key-shown");
  await expect(shown).toContainText("store it in a password manager");
  await expect(shown.locator("pre")).toContainText("fbk1:");
  keyId = ((await shown.locator("b").textContent()) ?? "").replace("Recovery key ", "").trim();
  await audit("backup key shown");
  await shown.getByRole("button", { name: "I stored it" }).click();
  await expect(keys.locator("tbody tr", { hasText: keyId })).toContainText("encrypts");

  await newSchedule("encrypted nightly", "backup");
  await pane().getByLabel("Encrypt the backups").check();
  const row = await saveAndRun("encrypted nightly");
  await expect(row.locator(".pill")).toHaveText("finished", { timeout: 60_000 });
  await row.click();
  const files = pane().locator(".mt-section", { hasText: "Backups of this schedule" });
  await expect(files.getByLabel(`Encrypted with key ${keyId}`)).toBeVisible();
  await expect(files.getByRole("button", { name: "Download decrypted" })).toBeVisible();
  await expect(pane().locator(".mt-section", { hasText: "History of this schedule" }).locator("tbody tr").first()).toContainText("encrypted");
});

test("restore drill: report with its checks, accessible in every theme", async () => {
  await newSchedule("weekly drill", "restore_drill");
  await pane().getByLabel("Backup schedule", { exact: true }).selectOption({ label: "encrypted nightly" });
  await expect(pane().getByLabel("Newest backup at most (hours old)")).toHaveValue("26");
  await audit("drill form");
  const row = await saveAndRun("weekly drill");
  await expect(row.locator(".pill")).toHaveText("finished", { timeout: 90_000 });
  await row.click();
  const report = pane().locator(".mt-section", { hasText: "Drill report" });
  await expect(report.locator(".pill")).toHaveText("passed");
  await expect(report).toContainText(`yes, key ${keyId}`);
  await expect(report.locator(".drill-checks")).toContainText("Online validation: no errors");
  await expect(report.locator(".drill-checks")).toContainText("Every table can be read (1 tables, 2 rows)");
  await expect(report.locator("tbody tr", { hasText: "ORDERS" })).toContainText("same");
  await expect(pane().locator(".mt-section", { hasText: "History of this schedule" }).locator("tbody tr").first()).toContainText("drill passed");
  for (const theme of ["light", "dark", "contrast"]) {
    await page.evaluate((th) => { document.documentElement.dataset.theme = th; }, theme);
    await audit(`drill report (${theme} theme)`);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
});
