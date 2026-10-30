import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { FB, expand, field, treeRow } from "./fixtures";

/**
 * Roadmap 2.2: versioned migrations. Scripts in a git repository; the tab shows them pending, apply waits for a
 * dry run, the dry run reports each script and the schema changes, then apply records them.
 */
test.describe.configure({ mode: "serial" });

const SERVER = `E2E Migrations ${Date.now().toString(36)}`;
let page: Page;
let serverId = "";
let repo = "";

function git(...args: string[]) {
  execFileSync("git", ["-C", repo, ...args], {
    env: { ...process.env, GIT_AUTHOR_NAME: "e2e", GIT_AUTHOR_EMAIL: "e2e@x", GIT_COMMITTER_NAME: "e2e", GIT_COMMITTER_EMAIL: "e2e@x" },
  });
}

test.beforeAll(async ({ browser, request }) => {
  repo = mkdtempSync(join(tmpdir(), "fbadmin-e2e-mig-"));
  mkdirSync(join(repo, "db"));
  writeFileSync(join(repo, "db", "V1__customers.sql"), "CREATE TABLE CUSTOMERS (ID INTEGER NOT NULL PRIMARY KEY, NAME VARCHAR(40));\n");
  writeFileSync(join(repo, "db", "V2__seed.sql"), "INSERT INTO CUSTOMERS VALUES (1, 'Ann');\n");
  git("init", "-q", "-b", "main");
  git("add", "-A");
  git("commit", "-q", "-m", "first");
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, {
    data: { name: "app", path: `${FB.dir}/fbadmin_e2emig_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  page = await (await browser.newContext()).newPage();
  page.on("dialog", (d) => d.accept());
  await page.goto("/");
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  const srv = (await (await request.get(`/api/servers/${serverId}`)).json()) as { databases: Array<{ id: string }> };
  for (const d of srv.databases ?? []) await request.post(`/api/servers/${serverId}/databases/${d.id}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

const pane = () => page.locator(".tab-pane:not(.hidden)");

async function audit(what: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
  expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), what).toEqual([]);
}

test("git source, dry run first, apply, recorded", async () => {
  await expand(page, SERVER);
  await treeRow(page, "app").click({ button: "right" });
  const ctx = page.locator(".ctx").first();
  const target = page.locator(".ctx button[role=menuitem]", { hasText: "Migrations…" }).first();
  if (!(await target.isVisible())) {
    for (const sub of await ctx.locator(":scope > .ctx-sub > button").all()) {
      await sub.click();
      if (await target.isVisible()) break;
    }
  }
  await target.click();
  await field(pane(), "Repository").fill(repo);
  await field(pane(), "Folder in the repository").fill("db");
  await audit("migration source form");
  await pane().getByRole("button", { name: "Save", exact: true }).click();

  const scripts = pane().getByRole("region", { name: "Migration scripts" });
  await expect(scripts.locator("tbody tr")).toHaveCount(2);
  await expect(scripts.locator("tbody tr").first()).toContainText("pending");
  await expect(pane().getByRole("button", { name: "Apply 2 pending" })).toBeDisabled();
  await expect(pane()).toContainText("Run a dry run first");

  await pane().getByRole("button", { name: "Dry run", exact: true }).click();
  const report = pane().locator(".mt-section", { hasText: "Schema changes" });
  await expect(report.locator(".pill")).toHaveText("passed", { timeout: 60_000 });
  await expect(report.locator(".mig-results")).toContainText("V1__customers.sql");
  await expect(report.locator(".mig-changes")).toContainText("CUSTOMERS");
  for (const theme of ["light", "dark", "contrast"]) {
    await page.evaluate((th) => { document.documentElement.dataset.theme = th; }, theme);
    await audit(`dry run report (${theme} theme)`);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });

  await pane().getByRole("button", { name: "Apply 2 pending" }).click();
  await expect(scripts.locator("tbody tr").nth(1)).toContainText("applied", { timeout: 60_000 });
  await expect(scripts.locator("tbody tr").first()).toContainText("applied");
  await expect(pane().locator(".card", { hasText: "Database version" })).toContainText("2");
  await expect(pane().getByRole("button", { name: "Apply 0 pending" })).toBeDisabled();
});
