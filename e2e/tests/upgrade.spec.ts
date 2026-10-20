import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { expand, FB, treeRow } from "./fixtures";

/**
 * Upgrade assistant (milestone 1.2): checks a database on the server at FB_PORT for an upgrade to
 * a newer Firebird, shows blockers with links into the tree, downloads the report, and, when a
 * newer server is reachable (UPGRADE_TARGET_PORT, default 3050 for a Firebird 3/4 source and 3056
 * for a Firebird 5 source), runs a trial restore there.
 */
test.describe.configure({ mode: "serial" });

const SERVER = "E2E Upgrade";
const TARGET = "E2E Upgrade target";
const DB_LABEL = "upgrade";
// a table named with a word that the next major versions reserve
const WORD: Record<number, string> = { 3: "WINDOW", 4: "CALL", 5: "LISTAGG" };

let page: Page;
let serverId = "";
let targetId = "";
let dbId = "";
let major = 0;
let targetMajor = 0;

async function engineMajor(request: import("@playwright/test").APIRequestContext, sid: string) {
  const r = await request.get(`/api/servers/${sid}/maintenance/capabilities`);
  return r.ok() ? (await r.json()).engine_major as number : 0;
}

test.beforeAll(async ({ browser, request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  major = await engineMajor(request, serverId);
  test.skip(!WORD[major], `no newer Firebird than ${major} to upgrade to`);
  const path = `${FB.dir}/fbadmin_e2eu_${Date.now().toString(36)}.fdb`;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, { data: { name: DB_LABEL, path } });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  const w = WORD[major];
  const sql = `CREATE TABLE ${w} (ID INTEGER NOT NULL PRIMARY KEY, V VARCHAR(10));
CREATE TABLE ITEMS (ID INTEGER NOT NULL PRIMARY KEY);
CREATE VIEW V_BAD AS SELECT ID FROM ${w};`;
  const r = await (await request.post(`/api/servers/${serverId}/databases/${dbId}/execute`, { data: { sql } })).json();
  expect(r.error, JSON.stringify(r.error)).toBeNull();
  const tport = Number(process.env.UPGRADE_TARGET_PORT ?? (major === 5 ? 3056 : 3050));
  targetId = (await (await request.post("/api/servers", {
    data: { name: TARGET, host: FB.host, port: tport, user: FB.user, password: FB.password },
  })).json()).id;
  targetMajor = await engineMajor(request, targetId);
  if (targetMajor <= major) {
    await request.delete(`/api/servers/${targetId}`);
    targetId = "";
  }
  page = await (await browser.newContext({ acceptDownloads: true })).newPage();
  await page.goto("/");
  await expand(page, SERVER);
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  if (targetId) await request.delete(`/api/servers/${targetId}`);
  if (!serverId) return;
  if (dbId) await request.post(`/api/servers/${serverId}/databases/${dbId}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

const pane = () => page.locator(".tab-pane:not(.hidden)");

async function audit(what: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
  expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), what).toEqual([]);
}

async function openAssistant() {
  await treeRow(page, DB_LABEL).click({ button: "right" });
  const ctx = page.locator(".ctx").first();
  await expect(ctx).toBeVisible();
  const item = page.locator(".ctx button[role=menuitem]", { hasText: "Upgrade assistant…" }).first();
  if (!(await item.isVisible())) {
    for (const sub of await ctx.locator(":scope > .ctx-sub > button").all()) {
      await sub.click();
      if (await item.isVisible()) break;
    }
  }
  await item.click();
  await expect(pane().locator("h2", { hasText: "Upgrade assistant" })).toBeVisible();
}

test("checks a database for a newer version and links findings to the tree", async () => {
  await openAssistant();
  const target = pane().getByLabel("Target version");
  await expect(target.locator("option")).toHaveCount(3 - (major - 3)); // only newer versions
  await target.selectOption(`${major === 5 ? 6 : major + 2}.0`);
  await audit("upgrade assistant form");
  await pane().locator("button", { hasText: /^\s*Run checks\s*$/ }).click();
  const blocker = pane().locator("details.upg-finding.blocker", { hasText: "Names that are reserved words" });
  await expect(blocker).toBeVisible();
  await expect(blocker).toHaveAttribute("open", "");
  await expect(pane().locator(".upg-count.blocker b")).not.toHaveText("0");
  await expect(pane().locator("h2", { hasText: "Migration plan" })).toBeVisible();
  await expect(pane().locator(".upg-cmd", { hasText: "gbak -b -g -v" })).toBeVisible();
  await expect(pane().locator("details.upg-finding", { hasText: "Code that uses words reserved" })).toContainText("V_BAD");
  await audit("upgrade report");

  // the table link selects the table in the Object Explorer
  await blocker.locator("a", { hasText: new RegExp(`^${WORD[major]}$`) }).first().click();
  await page.locator(".tab", { hasText: /^Properties$/ }).click();
  await expect(pane()).toContainText("Columns");
  await expect(pane()).toContainText(WORD[major]);

  // only blockers
  await page.locator(".tab", { hasText: "upgrade" }).first().click();
  await pane().locator(".upg-count.warning input").uncheck();
  await pane().locator(".upg-count.info input").uncheck();
  await expect(pane().locator("details.upg-finding.info")).toHaveCount(0);
  await pane().locator(".upg-count.info input").check();
  await pane().locator(".upg-count.warning input").check();
});

test("downloads the report as Markdown and HTML", async () => {
  for (const [label, ext] of [["Download Markdown", ".md"], ["Download HTML", ".html"]]) {
    const wait = page.waitForEvent("download");
    await pane().locator("button", { hasText: label }).click();
    const d = await wait;
    expect(d.suggestedFilename()).toMatch(new RegExp(`^upgrade_upgrade_fb\\d\\${ext}$`));
    const text = readFileSync((await d.path())!, "utf8");
    expect(text).toContain("Migration plan");
    expect(text).toContain(WORD[major]);
  }
});

test("trial restore on a newer server recompiles the code there", async () => {
  test.skip(!targetId, "no newer Firebird server reachable for the trial restore");
  test.setTimeout(180_000);
  await pane().getByLabel("Trial restore on").selectOption({ label: `${TARGET} (Firebird ${targetMajor})` });
  await expect(pane().getByLabel("Target version")).toHaveValue(`${targetMajor}.0`);
  await pane().getByText("Metadata only (faster)").click();
  await audit("upgrade assistant with trial options");
  await pane().locator("button", { hasText: "Run checks and trial restore" }).click();
  await expect(pane().locator("details.upg-finding", { hasText: "Trial restore on the target server" }))
    .toBeVisible({ timeout: 150_000 });
  const fails = pane().locator("details.upg-finding.blocker", { hasText: "Objects that do not compile on the target server" });
  if (major === 4 && targetMajor === 5) await expect(fails).toHaveCount(0); // Firebird 5 reserves nothing new here
  else await expect(fails).toContainText("V_BAD");
  await expect(pane().locator(".upg-facts")).toContainText({ 5: "13.1", 6: "14.0" }[targetMajor] ?? "");
  await audit("upgrade report with trial restore");
});
