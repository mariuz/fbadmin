import AxeBuilder from "@axe-core/playwright";
import { expect, request as pwRequest, test, type APIRequestContext, type Page } from "@playwright/test";
import { spawn, type ChildProcess } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { FB } from "./fixtures";

/**
 * Roadmap 2.3: shareable links in server mode. A link opened before signing in opens after the sign-in;
 * a link to a database the viewer may not read is refused, naming who shared it. This spec starts its own
 * fbadmin in server mode.
 */
test.describe.configure({ mode: "serial" });

const PORT = Number(process.env.E2E_LINKS_PORT ?? Number(process.env.E2E_PORT ?? 5106) + 1300);
const BASE = `http://127.0.0.1:${PORT}`;
const ADMIN = { user: "admin", password: "admin-password-e2e" };
const LENA = { user: "lena", password: "lena-password-e2e" };
let proc: ChildProcess | null = null;
let dataDir = "";
let admin: APIRequestContext;
let page: Page;
let serverId = "";
let salesId = "";
let objLink = "";
let hrLink = "";

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
  dataDir = mkdtempSync(join(tmpdir(), "fbadmin-e2e-links-"));
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
    data: { name: "sales", path: `${FB.dir}/fbadmin_e2elksm_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  salesId = (await db.json()).id;
  expect((await (await admin.post(`/api/servers/${serverId}/databases/${salesId}/execute`, {
    data: { sql: "CREATE TABLE ITEMS (ID INTEGER NOT NULL PRIMARY KEY)" } })).json()).error).toBeNull();
  const hr = (await (await admin.post(`/api/servers/${serverId}/databases`, { data: { name: "hr", path: `${FB.dir}/not_for_lena.fdb` } })).json()).id;
  expect((await admin.post("/api/admin/users", { data: { username: LENA.user, password: LENA.password, role: "user" } })).status()).toBe(201);
  expect((await admin.post("/api/admin/access/grants", {
    data: { subject_type: "user", subject: LENA.user, server_id: serverId, db_id: salesId, level: "read" } })).status()).toBe(201);
  objLink = (await (await admin.post("/api/links", {
    data: { kind: "object", server_id: serverId, database_id: salesId, otype: "tables", name: "ITEMS" } })).json()).id;
  hrLink = (await (await admin.post("/api/links", {
    data: { kind: "snapshot", title: "Salaries", server_id: serverId, database_id: hr,
      snapshot: { sql: "SELECT * FROM SALARIES", columns: [{ name: "NAME", type: "VARCHAR" }], rows: [["secret"]] } } })).json()).id;
  page = await (await browser.newContext()).newPage();
});

test.afterAll(async () => {
  await page?.context().close();
  await admin?.post(`/api/servers/${serverId}/databases/${salesId}/drop`).catch(() => undefined);
  await admin?.dispose();
  proc?.kill();
  if (dataDir) rmSync(dataDir, { recursive: true, force: true });
});

async function audit(what: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
  expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), what).toEqual([]);
}

test("a link opened before signing in opens after it", async () => {
  await page.goto(`${BASE}/#/link/${objLink}`);
  await page.getByLabel("User name or e-mail").fill(LENA.user);
  await page.getByLabel("Password", { exact: true }).fill(LENA.password);
  await page.locator("button[type=submit]", { hasText: "Sign in" }).click();
  await expect(page.locator(".toast").last()).toContainText("Opened ITEMS from a link by admin", { timeout: 15_000 });
  // lena has not saved a Firebird password for the shared server yet: fbadmin asks for it first
  const pw = page.getByRole("dialog", { name: /Connect to Shared FB/ });
  await pw.getByLabel("Password").fill(FB.password);
  await pw.getByRole("button", { name: "Connect" }).click();
  await expect(page.getByText("Table ITEMS", { exact: true })).toBeVisible({ timeout: 15_000 });
});

test("a link to a database the viewer may not read is refused", async () => {
  await page.goto(`${BASE}/#/link/${hrLink}`);
  const dlg = page.getByRole("dialog");
  await expect(dlg).toContainText("This link cannot be opened", { timeout: 15_000 });
  await expect(dlg).toContainText("Shared by admin");
  await expect(page.locator("body")).not.toContainText("secret");
  await audit("link refused");
});
