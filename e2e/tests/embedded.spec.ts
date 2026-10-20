import { expect, test } from "@playwright/test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { expand, field, setEditor, treeRow } from "./fixtures";

/**
 * Local databases through the embedded engine (features/embedded). Runs only when the fbadmin
 * under test has an engine: start Playwright with FBADMIN_EMBEDDED_DIR set (scripts/embedded-test-setup.sh).
 */
test("creates a local database file and queries it without a server", async ({ page, request }) => {
  const st = await (await request.get("/api/embedded/status")).json();
  test.skip(!st.available, `embedded engine not available: ${st.reason}`);
  const path = join(mkdtempSync(join(tmpdir(), "fbadmin-e2e-local-")), "notes.fdb");

  await page.goto("/");
  await page.locator(".topbar button", { hasText: "Local database" }).click();
  const dlg = page.locator(".modal");
  await expect(dlg.locator(".modal-head")).toHaveText("Create local database");
  await field(dlg, "Database file").locator("input").fill(path);
  await dlg.locator("button.primary", { hasText: "Create" }).click();
  await expect(dlg).toBeHidden();

  // the tree shows the "Local databases" entry with the new file, selected
  await expect(treeRow(page, "Local databases")).toContainText("embedded engine");
  await expand(page, "Local databases");
  await expect(treeRow(page, "notes")).toBeVisible();

  // DDL + query through the Query Tool (embedded engine, native driver)
  await treeRow(page, "notes").click();
  await page.locator(".topbar button", { hasText: "Query Tool" }).click();
  const qt = page.locator(".tab-pane:not(.hidden) .qt");
  await setEditor(page, qt, "CREATE TABLE NOTE (ID INTEGER PRIMARY KEY, TXT VARCHAR(40));\n"
    + "INSERT INTO NOTE VALUES (1, 'stored locally');\nSELECT TXT FROM NOTE;");
  await qt.locator("button", { hasText: "Execute" }).click();
  await expect(qt.locator(".qt-out table")).toContainText("stored locally");

  // server-only items are not offered for the local entry
  await treeRow(page, "Local databases").click({ button: "right" });
  const menu = page.locator(".ctx");
  await expect(menu).toContainText("Open local database file…");
  await expect(menu).not.toContainText("Edit server…");
  await expect(menu).not.toContainText("Trace sessions");
  await expect(menu).not.toContainText("Firebird users");
  await page.keyboard.press("Escape");
});
