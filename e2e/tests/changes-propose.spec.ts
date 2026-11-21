import AxeBuilder from "@axe-core/playwright";
import { expect, request as pwRequest, test, type APIRequestContext, type Page } from "@playwright/test";
import { spawn, type ChildProcess } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { FB, expand, setEditor, treeRow } from "./fixtures";

/**
 * Roadmap 2.5: propose from the Query Tool. In server mode, a user with read access whose DDL is refused gets
 * "Propose as a change request…", which opens the request form with the SQL filled in. This spec starts its
 * own fbadmin in server mode.
 */
test.describe.configure({ mode: "serial" });

const PORT = Number(process.env.E2E_PROPOSE_PORT ?? Number(process.env.E2E_PORT ?? 5106) + 1400);
const BASE = `http://127.0.0.1:${PORT}`;
const ADMIN = { user: "admin", password: "admin-password-e2e" };
const DEV = { user: "devon", password: "devon-password-e2e" };
let proc: ChildProcess | null = null;
let dataDir = "";
let admin: APIRequestContext;
let page: Page;
let serverId = "";
let dbId = "";

async function apiAs(user: { user: string; password: string }): Promise<APIRequestContext> {
  const tmp = await pwRequest.newContext({ baseURL: BASE });
  const r = await tmp.post("/api/auth/login", { data: { username: user.user, password: user.password } });
  expect(r.status(), await r.text()).toBe(200);
  const csrf = (await r.json()).csrf_token as string;
  const state = await tmp.storageState();
  await tmp.dispose();
  return pwRequest.newContext({ baseURL: BASE, storageState: state, extraHTTPHeaders: { "X-CSRF-Token": csrf } });
}

test.beforeAll(async ({ browser }) => {
  test.setTimeout(120_000);
  dataDir = mkdtempSync(join(tmpdir(), "fbadmin-e2e-propose-"));
  proc = spawn(process.env.FBADMIN_PYTHON ?? "python3",
    ["-m", "fbadmin", "--no-browser", "--host", "127.0.0.1", "--port", String(PORT), "--log-level", "warning"], {
      cwd: resolve(__dirname, "../../backend"),
      env: { ...process.env, FBADMIN_DATA_DIR: dataDir, FBADMIN_SERVER_MODE: "1", FBADMIN_ADMIN_USERNAME: ADMIN.user,
        FBADMIN_ADMIN_PASSWORD: ADMIN.password, FBADMIN_AUTH_CONFIG: join(dataDir, "auth.json") },
      stdio: "ignore",
    });
  const probe = await pwRequest.newContext({ baseURL: BASE });
  await expect.poll(async () => (await probe.get("/api/health").catch(() => null))?.status() ?? 0, { timeout: 60_000 }).toBe(200);
  await probe.dispose();
  admin = await apiAs(ADMIN);
  serverId = (await (await admin.post("/api/servers", {
    data: { name: "Shared FB", host: FB.host, port: FB.port, user: FB.user, password: FB.password, shared: true },
  })).json()).id;
  const db = await admin.post(`/api/servers/${serverId}/databases/create`, {
    data: { name: "appdb", path: `${FB.dir}/fbadmin_e2epropose_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  expect((await admin.post("/api/admin/users", { data: { username: DEV.user, password: DEV.password, role: "user" } })).status()).toBe(201);
  expect((await admin.post("/api/admin/access/grants", {
    data: { subject_type: "user", subject: DEV.user, server_id: serverId, db_id: dbId, level: "read" } })).status()).toBe(201);
  page = await (await browser.newContext()).newPage();
});

test.afterAll(async () => {
  await page?.context().close();
  await admin?.post(`/api/servers/${serverId}/databases/${dbId}/drop`).catch(() => undefined);
  await admin?.dispose();
  proc?.kill();
  if (dataDir) rmSync(dataDir, { recursive: true, force: true });
});

test("refused DDL can be proposed as a change request", async () => {
  await page.goto(BASE);
  await page.getByLabel("User name or e-mail").fill(DEV.user);
  await page.getByLabel("Password", { exact: true }).fill(DEV.password);
  await page.locator("button[type=submit]", { hasText: "Sign in" }).click();
  await expand(page, "Shared FB");
  const pw = page.getByRole("dialog", { name: /Connect to Shared FB/ });
  // devon has not saved a Firebird password for the shared server yet: fbadmin asks for it first
  await pw.getByLabel("Password").fill(FB.password);
  await pw.getByRole("button", { name: "Connect" }).click();
  await treeRow(page, "appdb").click();
  await page.locator(".topbar button", { hasText: "Query Tool" }).click();
  const qt = page.locator(".tab-pane:not(.hidden) .qt");
  await expect(qt).toBeVisible();
  const propose = qt.getByRole("button", { name: "Propose as a change request…" });
  await expect(propose).toHaveCount(0);
  const ddl = "CREATE TABLE INVOICES (ID INTEGER NOT NULL PRIMARY KEY)";
  await setEditor(page, qt, ddl);
  await qt.locator("button", { hasText: "Execute" }).click();
  await expect(qt).toContainText("Not allowed by your fbadmin permissions");
  await expect(propose).toBeVisible();
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
  expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`)).toEqual([]);
  await propose.click();

  const pane = page.locator(".tab-pane:not(.hidden)");
  await expect(pane.locator(".cr-editor .cm-content")).toHaveText(ddl);
  await pane.getByLabel("Title").fill("Invoices table");
  await pane.getByRole("button", { name: "Propose the change" }).click();
  await expect(pane.getByRole("list", { name: "Change requests" })).toContainText("Invoices table");
  const reqs = await (await admin.get("/api/changes", { params: { database_id: dbId } })).json();
  expect(reqs.map((c: { title: string; author: string }) => [c.title, c.author])).toEqual([["Invoices table", DEV.user]]);
});
