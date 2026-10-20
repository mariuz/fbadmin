import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { copyFileSync, mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { expand, FB, SCHEMA, treeRow } from "./fixtures";

/**
 * Milestone 2.0: ERD export to SVG and PNG, and the schema history in git (configure, snapshot,
 * change a table, snapshot again, look at the diff, the object at a commit and a migration script).
 * Set E2E_SHOT_DIR to keep a copy of the exported images.
 */
test.describe.configure({ mode: "serial" });

const SERVER = "E2E History";
const DB_LABEL = "history";
let page: Page;
let serverId = "";
let dbId = "";

test.beforeAll(async ({ browser, request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  const path = `${FB.dir}/fbadmin_e2eh_${Date.now().toString(36)}.fdb`;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, { data: { name: DB_LABEL, path } });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  const sql = SCHEMA + "\nCREATE TABLE ORDERS (ID INTEGER NOT NULL PRIMARY KEY, CUSTOMER_ID INTEGER NOT NULL REFERENCES CUSTOMER (ID), AMOUNT NUMERIC(12,2));\n";
  const r = await (await request.post(`/api/servers/${serverId}/databases/${dbId}/execute`, { data: { sql } })).json();
  expect(r.error, JSON.stringify(r.error)).toBeNull();
  page = await (await browser.newContext({ acceptDownloads: true })).newPage();
  await page.goto("/");
  await expand(page, SERVER);
  await expect(treeRow(page, DB_LABEL)).toBeVisible();
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  if (!serverId) return;
  await request.delete(`/api/servers/${serverId}/databases/${dbId}/schema-history`);
  await request.post(`/api/servers/${serverId}/databases/${dbId}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

const pane = () => page.locator(".tab-pane:not(.hidden)");

async function menu(rowLabel: string, item: string) {
  await treeRow(page, rowLabel).click({ button: "right" });
  const ctx = page.locator(".ctx").first();
  await expect(ctx).toBeVisible();
  const target = page.locator(".ctx button[role=menuitem]", { hasText: item }).first();
  if (!(await target.isVisible())) {
    for (const sub of await ctx.locator(":scope > .ctx-sub > button").all()) {
      await sub.click();
      if (await target.isVisible()) break;
    }
  }
  await target.click();
}

async function audit(what: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
  expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), what).toEqual([]);
}

async function download(button: string) {
  const dlg = page.locator(".modal");
  const [dl] = await Promise.all([page.waitForEvent("download"), dlg.locator("button", { hasText: button }).last().click()]);
  const file = await dl.path();
  const data = readFileSync(file!);
  if (process.env.E2E_SHOT_DIR) {
    mkdirSync(process.env.E2E_SHOT_DIR, { recursive: true });
    copyFileSync(file!, join(process.env.E2E_SHOT_DIR, dl.suggestedFilename()));
  }
  return { name: dl.suggestedFilename(), data };
}

function pngSize(buf: Buffer) {
  expect(buf.subarray(0, 8).toString("hex")).toBe("89504e470d0a1a0a");
  expect(buf.subarray(12, 16).toString("ascii")).toBe("IHDR");
  return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
}

test("ERD: export the whole diagram as SVG and PNG", async () => {
  await menu(DB_LABEL, "ERD diagram");
  await expect(pane().locator("svg.sch-erd-canvas")).toContainText("ORDERS");
  await pane().locator("button", { hasText: "Export image…" }).click();
  const dlg = page.locator(".modal");
  await expect(dlg).toContainText("Export diagram");
  await expect(dlg.getByRole("img", { name: "Preview" })).toBeVisible();
  await audit("ERD export dialog");
  if (process.env.E2E_SHOT_DIR) await page.screenshot({ path: join(process.env.E2E_SHOT_DIR, "erd-export-dialog.png") });

  // SVG, light colours on white: a standalone document with concrete colours and fonts
  const svg = await download("Download");
  expect(svg.name).toBe(`${DB_LABEL}-erd.svg`);
  const text = svg.data.toString("utf8");
  expect(text.startsWith("<?xml")).toBe(true);
  expect(text).not.toContain("var(--");
  expect(text).toContain("CUSTOMER");
  expect(text).toContain("ORDERS");
  expect(text).toMatch(/<\/title><rect [^>]*fill="#ffffff"/); // white background
  const parsed = await page.evaluate((s) => {
    const doc = new DOMParser().parseFromString(s, "image/svg+xml");
    const root = doc.documentElement;
    return { error: doc.querySelector("parsererror")?.textContent ?? null, tag: root.tagName, ns: root.namespaceURI,
      width: Number(root.getAttribute("width")), height: Number(root.getAttribute("height")), handles: doc.querySelectorAll("circle").length };
  }, text);
  expect(parsed.error).toBeNull();
  expect(parsed.tag).toBe("svg");
  expect(parsed.ns).toBe("http://www.w3.org/2000/svg");
  expect(parsed.width).toBeGreaterThan(300);
  expect(parsed.height).toBeGreaterThan(100);
  expect(parsed.handles).toBe(0); // editing handles are not exported

  // PNG at 1× and 2× with a title block
  await pane().locator("button", { hasText: "Export image…" }).click();
  await dlg.getByRole("button", { name: "PNG", exact: true }).click();
  await dlg.getByLabel("Title block (database name and date)").check();
  await dlg.getByRole("button", { name: "1×" }).click();
  await expect(dlg).toContainText(`${DB_LABEL}-erd.png`);
  const png1 = await download("Download");
  expect(png1.name).toBe(`${DB_LABEL}-erd.png`);
  const s1 = pngSize(png1.data);
  await pane().locator("button", { hasText: "Export image…" }).click();
  await dlg.getByRole("button", { name: "PNG", exact: true }).click();
  await dlg.getByLabel("Title block (database name and date)").check();
  await dlg.getByRole("button", { name: "2×" }).click();
  const png2 = await download("Download");
  const s2 = pngSize(png2.data);
  expect(s1.width).toBeGreaterThan(parsed.width - 1); // the title block adds height, not width
  expect(s1.height).toBeGreaterThan(parsed.height);
  expect(Math.abs(s2.width - 2 * s1.width)).toBeLessThanOrEqual(2);
  expect(Math.abs(s2.height - 2 * s1.height)).toBeLessThanOrEqual(2);

  // the visible area in the current theme with a transparent background
  await pane().locator("button", { hasText: "Export image…" }).click();
  await dlg.getByRole("button", { name: "Visible area" }).click();
  await dlg.getByRole("button", { name: "Current theme" }).click();
  await dlg.getByRole("button", { name: "Transparent" }).click();
  const view = await download("Download");
  expect(view.name).toBe(`${DB_LABEL}-erd.svg`);
  expect(view.data.toString("utf8")).not.toMatch(/<\/title><rect/); // no background rectangle
});

test("schema history: configure, snapshot, change a table, diff, migration", async ({ request }) => {
  await menu(DB_LABEL, "Schema history (git)…");
  await expect(pane()).toContainText("Schema history in git");
  await pane().getByLabel("Repository folder").fill("e2e-history");
  await pane().getByLabel("Committer name").fill("E2E Bot");
  await audit("schema history settings");
  await pane().locator("button.btn.primary", { hasText: "Save" }).click();
  await expect(pane()).toContainText("No snapshots yet");

  await pane().locator("button", { hasText: "Take snapshot" }).click();
  const commits = pane().locator(".hist-commit");
  await expect(commits).toHaveCount(1);
  await expect(commits.first()).toContainText("Schema snapshot of history");
  await expect(pane().locator(".hist-file", { hasText: "table CUSTOMER" })).toBeVisible();

  // nothing changed: no new commit
  await pane().locator("button", { hasText: "Take snapshot" }).click();
  await expect(page.locator(".toast", { hasText: "No schema changes" })).toBeVisible();
  await expect(commits).toHaveCount(1);

  const r = await (await request.post(`/api/servers/${serverId}/databases/${dbId}/execute`, {
    data: { sql: "ALTER TABLE CUSTOMER ADD EMAIL VARCHAR(80);\nCREATE TABLE NOTES (ID INTEGER NOT NULL, TXT VARCHAR(200));\nDROP VIEW V_RICH;" },
  })).json();
  expect(r.error, JSON.stringify(r.error)).toBeNull();
  await pane().locator("button", { hasText: "Take snapshot" }).click();
  await expect(commits).toHaveCount(2);
  await expect(commits.first()).toContainText("1 added, 1 changed, 1 dropped");
  await expect(commits.first()).toContainText("E2E Bot");
  const files = pane().locator(".hist-file");
  await expect(files.filter({ hasText: "table CUSTOMER" })).toBeVisible();
  await expect(files.filter({ hasText: "table NOTES" })).toBeVisible();
  await expect(files.filter({ hasText: "view V_RICH" })).toBeVisible();
  await files.filter({ hasText: "table CUSTOMER" }).click();
  const diff = pane().locator(".hist-diff");
  await expect(diff.locator(".hist-line.hist-add")).toContainText("EMAIL VARCHAR(80)");
  await audit("schema history diff");
  if (process.env.E2E_SHOT_DIR) await page.screenshot({ path: join(process.env.E2E_SHOT_DIR, "schema-history-diff.png") });

  await pane().locator("button", { hasText: "Show at this commit" }).click();
  await expect(pane().locator(".hist-subtitle")).toContainText("table CUSTOMER at commit");
  await expect(pane().locator(".cm-content")).toContainText("EMAIL VARCHAR(80)");
  await files.filter({ hasText: "view V_RICH" }).click();
  await pane().locator("button", { hasText: "Show before" }).click();
  await expect(pane().locator(".cm-content")).toContainText("CREATE VIEW V_RICH");

  // migration from the first snapshot to this one
  const first = await commits.nth(1).locator("code").textContent();
  const options = await pane().getByLabel("Migration script from").locator("option").allTextContents();
  await pane().getByLabel("Migration script from").selectOption({ label: options.find((o) => o.startsWith(first!.trim()))! });
  await pane().locator("button", { hasText: "Generate" }).click();
  const editor = pane().locator(".cm-content");
  await expect(editor).toContainText("ALTER TABLE CUSTOMER ADD EMAIL VARCHAR(80)");
  await expect(editor).toContainText("CREATE TABLE NOTES");
  await expect(editor).toContainText("DROP VIEW V_RICH");
  await audit("schema history migration");
  await pane().locator("button", { hasText: "Back to the changes" }).click();
  await expect(diff).toBeVisible();
});
