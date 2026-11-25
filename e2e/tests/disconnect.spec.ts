import { expect, test, type Page } from "@playwright/test";
import { FB, expand, treeRow } from "./fixtures";

/**
 * Issue #9: Disconnect really disconnects. Its tabs close, its node collapses, and using it again needs an
 * explicit Connect (a dialog, no silent reconnect).
 */
test.describe.configure({ mode: "serial" });

const SERVER = `E2E Disconnect ${Date.now().toString(36)}`;
let page: Page;
let serverId = "";
let dbId = "";

test.beforeAll(async ({ browser, request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, {
    data: { name: "discodb", path: `${FB.dir}/fbadmin_e2edc_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  page = await (await browser.newContext()).newPage();
  await page.goto("/");
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  await request.post(`/api/servers/${serverId}/databases/${dbId}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

async function menu(label: string, item: string) {
  await treeRow(page, label).click({ button: "right" });
  await page.locator(".ctx button[role=menuitem]", { hasText: new RegExp(`^${item}$`) }).first().click();
}

const dialog = () => page.locator(".modal", { hasText: "is disconnected" });

test("Disconnect a database: its tabs close, its node collapses, Connect is explicit", async () => {
  await expand(page, SERVER);
  await expand(page, "discodb");
  await expect(treeRow(page, "discodb")).toHaveAttribute("aria-expanded", "true");
  await treeRow(page, "discodb").click();
  await page.locator(".topbar button", { hasText: "Query Tool" }).click();
  await expect(page.locator(".tab-pane:not(.hidden) .qt")).toBeVisible();

  await menu("discodb", "Disconnect");
  await expect(page.locator(".qt")).toHaveCount(0);
  await expect(treeRow(page, "discodb")).toHaveAttribute("aria-expanded", "false");

  // expanding it again asks first (the password is saved: no password field)
  await expand(page, "discodb");
  await expect(dialog()).toContainText("discodb is disconnected");
  await expect(dialog().locator("input[type=password]")).toHaveCount(0);
  await dialog().getByRole("button", { name: "Connect" }).click();
  await expect(dialog()).toHaveCount(0);
  await expect(treeRow(page, "discodb")).toHaveAttribute("aria-expanded", "true");
});

test("Disconnect a server: Cancel keeps it disconnected; the menu offers Connect", async () => {
  await menu(SERVER, "Disconnect");
  await expect(treeRow(page, SERVER)).toHaveAttribute("aria-expanded", "false");
  await expand(page, SERVER); // even with a saved password, it asks
  await expect(dialog()).toContainText(`${SERVER} is disconnected`);
  await dialog().getByRole("button", { name: "Cancel" }).click();
  await expect(page.locator("[role=tree]").first()).toContainText("choose Connect");

  await menu(SERVER, "Connect");
  await expect.poll(async () => (await (await page.request.get("/api/servers", { headers: { "X-Fbadmin-Client": "ui" } })).json())
    .find((s: { id: string }) => s.id === serverId).disconnected).toBe(false);
});
