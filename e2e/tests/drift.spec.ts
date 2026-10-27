import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { FB } from "./fixtures";

/**
 * Roadmap 2.2: schema drift detection. A schema_drift schedule compares a database with its schema
 * history (git) or with another database; the drift report lists the differences with both DDL versions
 * and the script that makes the database match the baseline.
 */
test.describe.configure({ mode: "serial" });

const SERVER = "E2E Drift";
let page: Page;
let serverId = "";
const dbs: Record<string, string> = {};
const schedules: string[] = [];

test.beforeAll(async ({ browser, request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  for (const name of ["prod", "staging"]) {
    const path = `${FB.dir}/fbadmin_e2edr_${name}_${Date.now().toString(36)}.fdb`;
    const db = await request.post(`/api/servers/${serverId}/databases/create`, { data: { name, path } });
    expect(db.status(), await db.text()).toBe(201);
    dbs[name] = (await db.json()).id;
    const sql = "CREATE TABLE CUSTOMER (ID INTEGER NOT NULL PRIMARY KEY, NAME VARCHAR(40));\n";
    const r = await (await request.post(`/api/servers/${serverId}/databases/${dbs[name]}/execute`, { data: { sql } })).json();
    expect(r.error, JSON.stringify(r.error)).toBeNull();
  }
  const base = `/api/servers/${serverId}/databases/${dbs.prod}/schema-history`;
  const cfg = await request.put(base, { data: { repo_path: `e2e-drift-${Date.now().toString(36)}` } });
  expect(cfg.status(), await cfg.text()).toBe(200);
  const snap = await request.post(base + "/snapshot");
  expect(snap.status(), await snap.text()).toBe(200);
  // a change made after the recorded snapshot
  const r = await (await request.post(`/api/servers/${serverId}/databases/${dbs.prod}/execute`,
    { data: { sql: "CREATE TABLE TMP_IMPORT (ID INTEGER);\nALTER TABLE CUSTOMER ADD EMAIL VARCHAR(80);\n" } })).json();
  expect(r.error, JSON.stringify(r.error)).toBeNull();
  page = await (await browser.newContext()).newPage();
  await page.goto("/");
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  for (const id of schedules) await request.delete(`/api/schedules/${id}`);
  if (!serverId) return;
  await request.delete(`/api/servers/${serverId}/databases/${dbs.prod}/schema-history`);
  for (const id of Object.values(dbs)) await request.post(`/api/servers/${serverId}/databases/${id}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

const pane = () => page.locator(".tab-pane:not(.hidden)");

async function audit(what: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
  expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), what).toEqual([]);
}

async function newDriftSchedule(name: string) {
  await pane().locator("button", { hasText: "New schedule" }).click();
  await pane().getByLabel("Name", { exact: true }).fill(name);
  await pane().locator("select", { has: page.locator('option[value="schema_drift"]') }).selectOption("schema_drift");
  await pane().locator("select", { has: page.locator(`option:text-is("${SERVER}")`) }).first().selectOption({ label: SERVER });
  await pane().getByLabel("Database", { exact: true }).selectOption({ label: "prod" });
  await expect(pane().getByText("Drift check", { exact: true })).toBeVisible();
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

test("drift against the schema history: report, both DDL versions and the script", async () => {
  await page.locator(".topbar button", { hasText: "Schedules" }).click();
  await newDriftSchedule("prod drift");
  await expect(pane().getByLabel("Branch, tag or commit")).toBeVisible();
  await audit("drift schedule form");
  const row = await saveAndRun("prod drift");
  await expect(row.locator(".pill")).toHaveText("drift", { timeout: 60_000 });

  const report = pane().locator(".mt-section", { hasText: "Drift report" });
  await expect(report).toContainText("prod compared with its schema history");
  await expect(report.locator(".drift-bar .drift-status.extra")).toContainText("1");
  await expect(report.locator(".drift-bar .drift-status.changed")).toContainText("1");
  await report.getByRole("option", { name: /CUSTOMER/ }).click();
  await expect(report.locator(".sch-detail")).toContainText("EMAIL");
  for (const theme of ["light", "dark", "contrast"]) {
    await page.evaluate((th) => { document.documentElement.dataset.theme = th; }, theme);
    await audit(`drift report (${theme} theme)`);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
  await report.getByRole("tab", { name: "Script" }).click();
  await expect(report.locator(".cm-content")).toContainText("DROP TABLE TMP_IMPORT");
  // the history row shows the summary
  await expect(pane().locator(".mt-section", { hasText: "History of this schedule" }).locator("tbody tr").first())
    .toContainText("drift: 1 extra, 1 changed");
});

test("drift against another database, with ignored names", async () => {
  await newDriftSchedule("prod vs staging");
  await pane().getByLabel("Another database").check();
  await pane().getByLabel("Baseline database").selectOption({ label: "staging" });
  await pane().getByLabel("Ignore names").fill("TMP_*");
  await pane().getByLabel("Comments").check();
  const row = await saveAndRun("prod vs staging");
  // CUSTOMER still differs (the EMAIL column), TMP_IMPORT is ignored
  await expect(row.locator(".pill")).toHaveText("drift", { timeout: 60_000 });
  const report = pane().locator(".mt-section", { hasText: "Drift report" });
  await expect(report).toContainText("1 ignored");
  await expect(report.getByRole("option")).toHaveCount(1);
  await expect(report.getByRole("option", { name: /CUSTOMER/ })).toBeVisible();
  await report.getByRole("button", { name: "Open in Query Tool" }).click();
  await expect(page.locator(".tab-pane:not(.hidden) .cm-content")).toContainText("ALTER TABLE CUSTOMER DROP EMAIL");
});
