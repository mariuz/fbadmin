import AxeBuilder from "@axe-core/playwright";
import { expect, request as pwRequest, test, type Page } from "@playwright/test";
import { spawn, type ChildProcess } from "node:child_process";
import { createServer, type Server } from "node:http";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

/**
 * Roadmap 2.2: audit log forwarding. This spec starts its own fbadmin with auditing on (it is off in the
 * shared desktop-mode instance) and a local webhook receiver: a destination is added in the UI, an audited
 * change arrives at the receiver, and the destination shows what was sent.
 */
test.describe.configure({ mode: "serial" });

const PORT = Number(process.env.E2E_AUDIT_PORT ?? Number(process.env.E2E_PORT ?? 5106) + 1100);
const BASE = `http://127.0.0.1:${PORT}`;
let proc: ChildProcess | null = null;
let dataDir = "";
let hook: Server;
let hookUrl = "";
const received: Array<{ headers: Record<string, unknown>; body: any }> = [];
let page: Page;

test.beforeAll(async ({ browser }) => {
  hook = createServer((req, res) => {
    let data = "";
    req.on("data", (c) => (data += c));
    req.on("end", () => { received.push({ headers: req.headers, body: JSON.parse(data || "{}") }); res.end("ok"); });
  });
  await new Promise<void>((r) => hook.listen(0, "127.0.0.1", () => r()));
  hookUrl = `http://127.0.0.1:${(hook.address() as { port: number }).port}/audit`;
  dataDir = mkdtempSync(join(tmpdir(), "fbadmin-e2e-audit-"));
  proc = spawn(process.env.FBADMIN_PYTHON ?? "python3", ["-m", "fbadmin", "--no-browser", "--host", "127.0.0.1", "--port", String(PORT), "--log-level", "warning"], {
    cwd: resolve(__dirname, "../../backend"),
    env: { ...process.env, FBADMIN_DATA_DIR: dataDir, FBADMIN_AUDIT: "1", FBADMIN_AUDIT_FORWARD_INTERVAL: "1" },
    stdio: "ignore",
  });
  const api = await pwRequest.newContext({ baseURL: BASE });
  for (let i = 0; i < 100; i++) {
    try { if ((await api.get("/api/health")).ok()) break; } catch { /* starting */ }
    await new Promise((r) => setTimeout(r, 300));
  }
  await api.dispose();
  page = await (await browser.newContext({ baseURL: BASE })).newPage();
  await page.goto(BASE + "/");
});

test.afterAll(async () => {
  await page?.context().close();
  proc?.kill();
  hook?.close();
  if (dataDir) rmSync(dataDir, { recursive: true, force: true });
});

const pane = () => page.locator(".tab-pane:not(.hidden)");

async function audit(what: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
  expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), what).toEqual([]);
}

test("a webhook destination receives the audit log", async () => {
  await page.locator(".topbar button", { hasText: "Security" }).click();
  await page.locator(".user-menu-pop button", { hasText: "Audit log" }).click();
  await pane().getByRole("button", { name: "Forwarding…" }).click();
  await pane().getByRole("button", { name: "Add a destination" }).click();
  await pane().getByLabel("Name", { exact: true }).fill("SIEM");
  await pane().getByLabel("Send to").selectOption("webhook");
  await pane().getByLabel("URL", { exact: true }).fill(hookUrl);
  await pane().getByLabel("Signing secret (optional)").fill("e2e-secret");
  await audit("forwarding form");
  await pane().locator(".fwd-form").getByRole("button", { name: "Save" }).click();

  // adding the destination is itself an audit entry: it arrives, signed
  await expect.poll(() => received.length, { timeout: 15_000 }).toBeGreaterThan(0);
  const first = received[0];
  expect(String(first.headers["x-fbadmin-signature"])).toMatch(/^sha256=[0-9a-f]{64}$/);
  expect(first.body.entries[0].statement).toContain("added webhook destination 'SIEM'");
  const row = pane().locator(".fwd tbody tr", { hasText: "SIEM" });
  await expect(row).toContainText("HTTP 200", { timeout: 15_000 });
  await expect(row.locator("td").nth(3)).toHaveText("0");
  await row.getByRole("button", { name: "Test" }).click();
  await expect(page.locator(".toast").last()).toContainText("Test message sent");
  for (const theme of ["light", "dark", "contrast"]) {
    await page.evaluate((th) => { document.documentElement.dataset.theme = th; }, theme);
    await audit(`forwarding (${theme} theme)`);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
});
