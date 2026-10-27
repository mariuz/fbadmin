import AxeBuilder from "@axe-core/playwright";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { FB, field, expand, treeRow } from "./fixtures";

/**
 * Roadmap 2.2: the replication setup wizard. On the usual test server the plan, the replication.conf block
 * (written into a local file) and a setup that stops because the primary writes no journal. With the two
 * servers of scripts/replication-test-servers.sh (FB_REPL=1) the whole asynchronous setup through the UI:
 * both replication.conf files, the copy, the replica server restart and the check row arriving.
 */
test.describe.configure({ mode: "serial" });

const REPL = process.env.FB_REPL === "1";
const P_PORT = Number(process.env.FB_REPL_PRIMARY_PORT ?? 3060);
const R_PORT = Number(process.env.FB_REPL_REPLICA_PORT ?? 3061);
const REPL_DIR = process.env.FB_REPL_DIR ?? "/tmp/fbadmin-repl";
const DATA = "/var/lib/firebird/data";

let page: Page;
const created: Array<{ sid: string; dbid?: string }> = [];

async function server(request: APIRequestContext, name: string, port: number) {
  const sid = (await (await request.post("/api/servers", {
    data: { name, host: FB.host, port, user: FB.user, password: FB.password },
  })).json()).id as string;
  created.push({ sid });
  return sid;
}

async function database(request: APIRequestContext, sid: string, label: string, path: string, sql: string) {
  const db = await request.post(`/api/servers/${sid}/databases/create`, { data: { name: label, path } });
  expect(db.status(), await db.text()).toBe(201);
  const dbid = (await db.json()).id as string;
  created.push({ sid, dbid });
  const r = await (await request.post(`/api/servers/${sid}/databases/${dbid}/execute`, { data: { sql } })).json();
  expect(r.error, JSON.stringify(r.error)).toBeNull();
  return dbid;
}

test.beforeAll(async ({ browser }) => {
  page = await (await browser.newContext()).newPage();
  await page.goto("/");
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  const all = (await (await request.get("/api/servers")).json()) as Array<{ id: string; databases: Array<{ id: string; path: string }> }>;
  for (const { sid } of created.filter((c) => !c.dbid)) {
    for (const d of all.find((s) => s.id === sid)?.databases ?? []) await request.post(`/api/servers/${sid}/databases/${d.id}/drop`);
    await request.delete(`/api/servers/${sid}`);
  }
});

const pane = () => page.locator(".tab-pane:not(.hidden)");

async function audit(what: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
  expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), what).toEqual([]);
}

async function openWizard(serverName: string, dbLabel: string) {
  await page.reload();
  await expand(page, serverName);
  await treeRow(page, dbLabel).click({ button: "right" });
  const ctx = page.locator(".ctx").first();
  await expect(ctx).toBeVisible();
  const target = page.locator(".ctx button[role=menuitem]", { hasText: "Set up a replica…" }).first();
  if (!(await target.isVisible())) {
    for (const sub of await ctx.locator(":scope > .ctx-sub > button").all()) {
      await sub.click();
      if (await target.isVisible()) break;
    }
  }
  await target.click();
  await expect(pane().getByRole("heading", { name: "1. The replica" })).toBeVisible();
}

test("plan, replication.conf block written into a file, and a primary that writes no journal", async ({ request }) => {
  const name = `E2E Repl ${Date.now().toString(36)}`;
  const sid = await server(request, name, FB.port);
  const path = `${FB.dir}/fbadmin_e2erepl_${Date.now().toString(36)}.fdb`;
  await database(request, sid, "main", path, "CREATE TABLE ORDERS (ID INTEGER NOT NULL PRIMARY KEY);\nCREATE TABLE NOTES (TXT VARCHAR(20));\n");
  await openWizard(name, "main");
  await field(pane(), "Replica server").selectOption({ label: `${name} (${FB.host}:${FB.port})` });
  await expect(field(pane(), "Replica file (on that server)")).toHaveValue(path.replace(/\.fdb$/, "_replica.fdb"));
  await pane().getByRole("button", { name: "Check the plan" }).click();
  const checks = pane().locator(".rs-checks");
  await expect(checks).toContainText("Tables without a primary or unique key");
  await expect(checks).toContainText("NOTES");
  const block = pane().getByLabel(`replication.conf block for ${path}`);
  await expect(block).toContainText(`database = ${path}`);
  await expect(block).toContainText("journal_archive_directory");
  for (const theme of ["light", "dark", "contrast"]) {
    await page.evaluate((th) => { document.documentElement.dataset.theme = th; }, theme);
    await audit(`replica setup plan (${theme} theme)`);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });

  // fbadmin writes the block into a replication.conf it can reach
  const conf = join(mkdtempSync(join(tmpdir(), "fbadmin-e2e-")), "replication.conf");
  writeFileSync(conf, "database\n{\n}\n");
  await pane().getByLabel("Or write it into").first().fill(conf);
  await pane().getByRole("button", { name: "Write", exact: true }).first().click();
  await expect(page.locator(".toast").last()).toContainText("Block added to");
  expect(readFileSync(conf, "utf8")).toContain(`database = ${path}\n{`);
  await expect(pane().getByLabel("The block is in replication.conf").first()).toBeChecked();

  // that file is not the server's: the primary writes no journal and the setup stops before the copy
  await pane().getByRole("button", { name: "Start the setup" }).click();
  await expect(pane().locator(".pill.failed")).toBeVisible({ timeout: 60_000 });
  await expect(pane()).toContainText("The primary writes no journal");
  await expect(pane().getByRole("button", { name: "Try again" })).toBeVisible();
});

test("asynchronous replica end to end through the UI", async ({ request }) => {
  test.skip(!REPL, "needs scripts/replication-test-servers.sh (FB_REPL=1)");
  test.setTimeout(240_000);
  const run = Date.now().toString(36);
  const primary = `E2E Primary ${run}`, replica = `E2E Replica ${run}`;
  const psid = await server(request, primary, P_PORT);
  await server(request, replica, R_PORT);
  const path = `${DATA}/e2e_repl_${run}.fdb`;
  await database(request, psid, "sales", path, "CREATE TABLE ORDERS (ID INTEGER NOT NULL PRIMARY KEY, NOTE VARCHAR(30));\nINSERT INTO ORDERS VALUES (1, 'before');\n");
  execFileSync("docker", ["exec", "-u", "firebird", "fbrepl-p", "mkdir", "-p", `/repl/${run}/journal`, `/repl/${run}/archive`]);

  await openWizard(primary, "sales");
  await field(pane(), "Replica server").selectOption({ label: `${replica} (${FB.host}:${R_PORT})` });
  await field(pane(), "Journal directory").fill(`/repl/${run}/journal`);
  await field(pane(), "Archive directory").fill(`/repl/${run}/archive`);
  await field(pane(), "Archive after (seconds)").fill("2");
  await field(pane(), "Copy directory (primary server)").fill(`/repl/${run}`);
  await pane().getByRole("button", { name: "Check the plan" }).click();
  await expect(pane().getByRole("heading", { name: "2. replication.conf of the primary server" })).toBeVisible();
  await pane().getByLabel("Or write it into").first().fill(`${REPL_DIR}/primary/replication.conf`);
  await pane().getByRole("button", { name: "Write", exact: true }).first().click();
  await expect(page.locator(".toast").last()).toContainText("Block added to");
  await pane().getByRole("button", { name: "Start the setup" }).click();
  await expect(pane().locator(".pill.finished")).toBeVisible({ timeout: 120_000 });
  await expect(pane().locator(".log")).toContainText("Journal active");

  const step4 = pane().locator(".mt-section", { hasText: "4. replication.conf of the replica server" });
  await expect(step4).toBeVisible();
  await step4.getByLabel("Or write it into").fill(`${REPL_DIR}/replica/replication.conf`);
  await step4.getByRole("button", { name: "Write", exact: true }).click();
  await expect(step4.getByLabel("The block is in replication.conf")).toBeChecked();
  execFileSync("docker", ["restart", "fbrepl-r"]);
  await new Promise((r) => setTimeout(r, 5000));
  await step4.getByLabel("The replica's Firebird server was restarted").check();
  const step5 = pane().locator(".mt-section", { hasText: "5. Check that changes arrive" });
  await step5.getByRole("button", { name: "Send a check row" }).click();
  await expect(step5.locator(".rs-ok")).toContainText("Replication works", { timeout: 150_000 });
  await audit("replica setup finished");
  // the replica is registered in fbadmin
  const servers = (await (await request.get("/api/servers")).json()) as Array<{ name: string; databases: Array<{ name: string }> }>;
  expect(servers.find((s) => s.name === replica)?.databases.map((d) => d.name)).toEqual(["sales (replica)"]);
});
