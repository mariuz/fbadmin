import AxeBuilder from "@axe-core/playwright";
import { expect, request as pwRequest, test, type APIRequestContext, type Browser, type Page } from "@playwright/test";
import { spawn, type ChildProcess } from "node:child_process";
import { createHmac } from "node:crypto";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { expand, FB, treeRow } from "./fixtures";

/**
 * Milestone 2.0 — server-mode security. This spec starts its own fbadmin in server mode (the shared
 * instance of the other specs runs in desktop mode): a restricted user sees only the database granted
 * to them, enrols two-factor authentication and signs in with a generated TOTP code, and creates a
 * read-only API token that the spec then uses through `request`.
 */
test.describe.configure({ mode: "serial" });

// a second fbadmin (server mode) next to the shared desktop-mode one: E2E_PORT + 1000 unless set
const PORT = Number(process.env.E2E_SERVER_MODE_PORT ?? Number(process.env.E2E_PORT ?? 5106) + 1000);
const BASE = `http://127.0.0.1:${PORT}`;
const ADMIN = { user: "admin", password: "admin-password-e2e" };
const RITA = { user: "rita", password: "rita-password-e2e" };
const SERVER = "Shared FB";
const SCHEMA = "CREATE TABLE ITEMS (ID INTEGER NOT NULL PRIMARY KEY, NAME VARCHAR(20));\nINSERT INTO ITEMS VALUES (1, 'one');";

let proc: ChildProcess | null = null;
let dataDir = "";
let admin: APIRequestContext;
let serverId = "";
const dbIds: Record<string, string> = {};
let dbPath = "";
let page: Page;
let mfaSecret = "";
let token = "";

// ---------------------------------------------------------------- TOTP (RFC 6238) for the spec
function base32(s: string): Buffer {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const c of s.replace(/[\s=]/g, "").toUpperCase()) bits += alphabet.indexOf(c).toString(2).padStart(5, "0");
  const bytes: number[] = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2));
  return Buffer.from(bytes);
}
function totp(secret: string, at = Date.now() / 1000): string {
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(at / 30)));
  const mac = createHmac("sha1", base32(secret)).update(counter).digest();
  const off = mac[mac.length - 1] & 0xf;
  return String((mac.readUInt32BE(off) & 0x7fffffff) % 1_000_000).padStart(6, "0");
}

async function apiAs(user: { user: string; password: string }): Promise<APIRequestContext> {
  // log in, keep the session cookie, and send the CSRF token on every request
  const tmp = await pwRequest.newContext({ baseURL: BASE });
  const r = await tmp.post("/api/auth/login", { data: { username: user.user, password: user.password } });
  expect(r.status(), await r.text()).toBe(200);
  const csrf = (await r.json()).csrf_token as string;
  const state = await tmp.storageState();
  await tmp.dispose();
  return pwRequest.newContext({ baseURL: BASE, storageState: state, extraHTTPHeaders: { "X-CSRF-Token": csrf } });
}

async function audit(what: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
  expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), what).toEqual([]);
}

async function uiLogin(user: { user: string; password: string }) {
  await page.goto(BASE + "/");
  await page.getByLabel("User name or e-mail").fill(user.user);
  await page.getByLabel("Password", { exact: true }).fill(user.password);
  await page.locator("button[type=submit]", { hasText: "Sign in" }).click();
}

async function userMenu(item: string) {
  await page.locator(".user-menu > button.tb").click();
  await page.locator(".user-menu-pop button", { hasText: item }).click();
}

const pane = () => page.locator(".tab-pane:not(.hidden)");

test.beforeAll(async ({ browser }: { browser: Browser }) => {
  test.setTimeout(120_000);
  const busy = await pwRequest.newContext({ baseURL: BASE });
  const taken = await busy.get("/api/health").then(() => true, () => false);
  await busy.dispose();
  expect(taken, `port ${PORT} is in use - set E2E_SERVER_MODE_PORT`).toBe(false);
  dataDir = mkdtempSync(join(tmpdir(), "fbadmin-e2e-sm-"));
  proc = spawn(process.env.FBADMIN_PYTHON ?? "python3",
    ["-m", "fbadmin", "--no-browser", "--host", "127.0.0.1", "--port", String(PORT), "--log-level", "warning"], {
      cwd: resolve(__dirname, "../../backend"),
      env: { ...process.env, FBADMIN_DATA_DIR: dataDir, FBADMIN_SERVER_MODE: "1", FBADMIN_ADMIN_USERNAME: ADMIN.user,
        FBADMIN_ADMIN_PASSWORD: ADMIN.password, FBADMIN_AUTH_CONFIG: join(dataDir, "auth.json") },
      stdio: "inherit",
    });
  const probe = await pwRequest.newContext({ baseURL: BASE });
  await expect.poll(async () => (await probe.get("/api/health").catch(() => null))?.status() ?? 0, { timeout: 60_000 }).toBe(200);
  await probe.dispose();

  admin = await apiAs(ADMIN);
  const srv = await admin.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password, shared: true },
  });
  expect(srv.status(), await srv.text()).toBe(201);
  serverId = (await srv.json()).id;
  dbPath = `${FB.dir}/fbadmin_e2e_acc_${Date.now().toString(36)}.fdb`;
  const db = await admin.post(`/api/servers/${serverId}/databases/create`, { data: { name: "sales", path: dbPath } });
  expect(db.status(), await db.text()).toBe(201);
  dbIds.sales = (await db.json()).id;
  const ex = await (await admin.post(`/api/servers/${serverId}/databases/${dbIds.sales}/execute`, { data: { sql: SCHEMA } })).json();
  expect(ex.error).toBeNull();
  const hr = await admin.post(`/api/servers/${serverId}/databases`, { data: { name: "hr", path: `${FB.dir}/not_for_rita.fdb` } });
  dbIds.hr = (await hr.json()).id;
  const u = await admin.post("/api/admin/users", { data: { username: RITA.user, password: RITA.password } });
  expect(u.status()).toBe(201);
  page = await (await browser.newContext()).newPage();
});

test.afterAll(async () => {
  await page?.context().close();
  if (admin && serverId && dbIds.sales) await admin.post(`/api/servers/${serverId}/databases/${dbIds.sales}/drop`).catch(() => undefined);
  await admin?.dispose();
  proc?.kill();
  if (dataDir) rmSync(dataDir, { recursive: true, force: true });
});

test("administrator grants a user read access to one database", async () => {
  await uiLogin(ADMIN);
  await userMenu("Permissions & two-factor");
  await expect(pane()).toContainText("No grants: every user works as in earlier versions.");
  await pane().locator("button", { hasText: "New grant" }).click();
  await pane().getByLabel("Who").selectOption({ label: RITA.user });
  await pane().getByLabel("Server", { exact: true }).selectOption({ label: SERVER });
  await pane().getByLabel("Database", { exact: true }).selectOption({ label: "sales" });
  await pane().getByLabel("Level", { exact: true }).selectOption("read");
  await pane().locator("button.btn.primary", { hasText: "Add grant" }).click();
  const row = pane().locator("tr", { hasText: RITA.user }).first();
  await expect(row).toContainText("sales");
  await pane().getByLabel("User", { exact: true }).selectOption({ label: RITA.user });
  await expect(pane().locator(".access-effective")).toContainText("sales");
  await expect(pane().locator(".access-effective")).not.toContainText("hr");
  await audit("permissions admin tab");
  await userMenu("Sign out");
  await expect(page.getByLabel("User name or e-mail")).toBeVisible();
});

test("restricted user sees only the granted database", async () => {
  await uiLogin(RITA);
  await expect(treeRow(page, SERVER)).toBeVisible();
  await expand(page, SERVER);
  // passwords of shared servers are personal: rita enters her own Firebird password
  const dlg = page.locator(".modal", { hasText: `Connect to ${SERVER}` });
  await dlg.getByLabel("Password").fill(FB.password);
  await dlg.locator("button", { hasText: "Connect" }).click();
  await expect(treeRow(page, "sales")).toBeVisible();
  await expect(treeRow(page, "hr")).toHaveCount(0);
  await userMenu("Sign-in & API tokens");
  await expect(pane().locator(".access-effective")).toContainText("sales");
  await expect(pane().locator(".access-effective .pill.level-read").first()).toBeVisible();
  await audit("sign-in & API tokens tab");
});

test("two-factor enrolment, then sign-in with a TOTP code", async () => {
  await pane().locator("button", { hasText: "Set up two-factor authentication" }).click();
  await expect(pane().locator("svg.qr-code")).toBeVisible();
  mfaSecret = (await pane().getByTestId("mfa-secret").innerText()).replace(/\s/g, "");
  expect(mfaSecret).toMatch(/^[A-Z2-7]{32}$/);
  await audit("two-factor enrolment");
  await pane().getByLabel("Code shown by the app").fill(totp(mfaSecret));
  await pane().locator("button.btn.primary", { hasText: "Turn on" }).click();
  await expect(pane().getByTestId("recovery-codes").locator("li")).toHaveCount(10);
  await expect(pane()).toContainText("10 recovery codes left");

  await userMenu("Sign out");
  await uiLogin(RITA);
  await expect(page.locator(".auth-card h2")).toHaveText("Two-factor authentication");
  await audit("two-factor sign-in step");
  // the enrolment code cannot be replayed; the next time step's code is accepted (±1 step window)
  await page.getByLabel("Authentication code").fill(totp(mfaSecret, Date.now() / 1000 + 30));
  await page.locator("button[type=submit]", { hasText: "Verify" }).click();
  await expect(treeRow(page, SERVER)).toBeVisible();
});

test("read-only API token used from a script", async () => {
  // rita saves her own Firebird password for the shared server (passwords of shared servers are personal)
  const rita = page.context().request;
  const csrf = (await page.context().cookies()).find((c) => c.name === "fbadmin_csrf")?.value ?? "";
  const r = await rita.put(`${BASE}/api/servers/${serverId}`, {
    data: { password: FB.password, save_password: true }, headers: { "X-CSRF-Token": csrf },
  });
  expect(r.status(), await r.text()).toBe(200);

  await userMenu("Sign-in & API tokens");
  await pane().locator("button", { hasText: "New token" }).click();
  await pane().getByLabel("Name", { exact: true }).fill("e2e report");
  await pane().getByLabel("Scope").selectOption("read-only");
  await pane().locator("button.btn.primary", { hasText: "Create token" }).click();
  token = (await pane().getByTestId("new-token").innerText()).trim();
  expect(token).toMatch(/^fbat_/);
  await expect(pane().locator("tr", { hasText: "e2e report" })).toContainText("read-only");
  await audit("token created");

  const script = await pwRequest.newContext({ baseURL: BASE, extraHTTPHeaders: { Authorization: `Bearer ${token}` } });
  try {
    const servers = await (await script.get("/api/servers")).json();
    expect(servers.map((s: { name: string }) => s.name)).toEqual([SERVER]);
    expect(servers[0].databases.map((d: { name: string }) => d.name)).toEqual(["sales"]);
    const q = await script.post(`/api/servers/${serverId}/databases/${dbIds.sales}/execute`, { data: { sql: "SELECT NAME FROM ITEMS" } });
    expect(q.status(), await q.text()).toBe(200);
    expect((await q.json()).error).toBeNull();
    const w = await script.post(`/api/servers/${serverId}/databases/${dbIds.sales}/execute`, { data: { sql: "DELETE FROM ITEMS" } });
    expect(w.status()).toBe(403);
    expect(await script.get(`/api/servers/${serverId}/databases/${dbIds.hr}/info`).then((x) => x.status())).toBe(404);
  } finally {
    await script.dispose();
  }
  // revoking the token in the UI stops the script
  page.once("dialog", (d) => d.accept());
  await pane().locator("tr", { hasText: "e2e report" }).locator("button", { hasText: "Revoke" }).click();
  await expect(pane().locator("tr", { hasText: "e2e report" })).toContainText("revoked");
  const after = await pwRequest.newContext({ baseURL: BASE, extraHTTPHeaders: { Authorization: `Bearer ${token}` } });
  expect((await after.get("/api/servers")).status()).toBe(401);
  await after.dispose();
});
