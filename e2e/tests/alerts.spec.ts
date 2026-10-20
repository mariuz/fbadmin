import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { createServer, type Server } from "node:http";
import { FB } from "./fixtures";

/**
 * Milestone 1.1 UI: alert channels and rules (a test message is delivered to a local webhook
 * receiver), backup storage targets, and the schedule options for test restores and uploads.
 */
test.describe.configure({ mode: "serial" });

const SERVER = "E2E Alerts";
let page: Page;
let serverId = "";
let receiver: Server;
const received: Array<{ url: string; body: any; signature?: string }> = [];
let hookUrl = "";

test.beforeAll(async ({ browser, request }) => {
  receiver = createServer((req, res) => {
    let data = "";
    req.on("data", (c) => (data += c));
    req.on("end", () => {
      received.push({ url: req.url ?? "", body: JSON.parse(data || "{}"), signature: req.headers["x-fbadmin-signature"] as string });
      res.end("ok");
    });
  });
  await new Promise<void>((r) => receiver.listen(0, "127.0.0.1", r));
  hookUrl = `http://127.0.0.1:${(receiver.address() as { port: number }).port}/hook`;
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  page = await (await browser.newContext()).newPage();
  await page.goto("/");
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  receiver?.close();
  for (const r of await (await request.get("/api/alerts/rules")).json()) await request.delete(`/api/alerts/rules/${r.id}`);
  for (const c of await (await request.get("/api/alerts/channels")).json()) await request.delete(`/api/alerts/channels/${c.id}`);
  for (const tg of await (await request.get("/api/storage/targets")).json()) await request.delete(`/api/storage/targets/${tg.id}`);
  if (serverId) await request.delete(`/api/servers/${serverId}`);
});

const pane = () => page.locator(".tab-pane:not(.hidden)");

async function audit(what: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
  expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), what).toEqual([]);
}

test("alert channel: create, send a test message to a webhook", async () => {
  await page.locator(".topbar button", { hasText: "Alerts" }).click();
  await expect(pane()).toContainText("Alert channels");
  await pane().locator("button", { hasText: "New channel" }).click();
  await pane().getByLabel("Name", { exact: true }).fill("ops hook");
  await pane().getByLabel("Kind").selectOption("webhook");
  await pane().getByLabel("URL", { exact: true }).fill(hookUrl);
  await pane().getByLabel("Signing secret (optional)").fill("topsecret");
  await pane().locator("button.btn.primary", { hasText: "Save" }).click();
  const row = pane().locator("tr", { hasText: "ops hook" });
  await expect(row).toContainText("URL set · signed");
  await row.locator("button", { hasText: "Send test" }).click();
  await expect.poll(() => received.length).toBe(1);
  expect(received[0].body.title).toBe("Test alert from fbadmin");
  expect(received[0].signature).toMatch(/^sha256=[0-9a-f]{64}$/);
  // the secret is not sent back to the browser
  await row.locator("button", { hasText: "Edit" }).click();
  await expect(pane().getByLabel("URL", { exact: true })).toHaveValue("********");
  await pane().locator("button", { hasText: "Cancel" }).click();
  await audit("alerts tab");
});

test("alert rule: condition rule needs a database and shows its state after a check", async () => {
  await pane().locator("button", { hasText: "New rule" }).click();
  await pane().getByLabel("When").selectOption("database.unreachable");
  await pane().getByLabel("Server / database").selectOption({ label: SERVER });
  // the server has no database registered yet: Save stays disabled
  await expect(pane().locator("button.btn.primary", { hasText: "Save" })).toBeDisabled();
  await pane().locator("button", { hasText: "Cancel" }).click();

  await pane().locator("button", { hasText: "New rule" }).click();
  await pane().getByLabel("Name", { exact: true }).fill("jobs failed");
  await pane().locator("button.btn.primary", { hasText: "Save" }).click();
  const row = pane().locator("tr", { hasText: "jobs failed" });
  await expect(row).toContainText("A scheduled job failed");
  await expect(row).toContainText("ops hook");
  await audit("alerts tab with a rule");
});

test("backup storage tab and schedule options", async () => {
  await page.locator(".topbar button", { hasText: "Schedules" }).click();
  await pane().locator("button", { hasText: "Backup storage" }).click();
  await expect(pane()).toContainText("No storage targets yet.");
  await pane().locator("button", { hasText: "New target" }).click();
  await pane().getByLabel("Name", { exact: true }).fill("offsite");
  await pane().getByLabel("Bucket *").fill("fbadmin-backups");
  await pane().getByLabel("Access key ID *").fill("AKIDEXAMPLE");
  await pane().getByLabel("Secret access key *").fill("env:FBADMIN_E2E_S3_SECRET");
  await pane().getByLabel("Endpoint (S3-compatible services)").fill("http://127.0.0.1:9");
  await expect(pane().getByLabel("Path-style URLs (needed by most S3-compatible services)")).toBeChecked();
  await pane().locator("button.btn.primary", { hasText: "Save" }).click();
  await expect(pane().locator("tr", { hasText: "offsite" })).toContainText("fbadmin-backups @ http://127.0.0.1:9");
  await audit("backup storage tab");

  await page.locator(".tab", { hasText: /^Schedules$/ }).click();
  await pane().locator("button", { hasText: "New schedule" }).click();
  await pane().getByText("Test-restore each backup").click();
  await expect(pane().getByLabel("Test-restore path on the server")).toBeVisible();
  await pane().getByLabel("Upload to cloud storage").selectOption({ label: "offsite (s3)" });
  await expect(pane().getByLabel("Keep last N in the cloud")).toBeVisible();
  await audit("schedule form with test restore and upload");
  await pane().locator("button", { hasText: "Cancel" }).click();
});
