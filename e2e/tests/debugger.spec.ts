import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { expand, FB, treeRow } from "./fixtures";

/**
 * Milestone 1.2 UI: PSQL debugging help — trace a procedure step by step, save the run as a test case
 * and run it, trace an EXECUTE BLOCK typed in, and the per-line profiler columns (Firebird 5).
 */
test.describe.configure({ mode: "serial" });

const SERVER = "E2E Debugger";
const DB_LABEL = "debugger";
let page: Page;
let serverId = "";
let dbId = "";

const SCHEMA = `
CREATE TABLE AUDIT_T (ID INTEGER, MSG VARCHAR(40));
SET TERM ^ ;
CREATE PROCEDURE P_TRACE (N INTEGER, PREFIX VARCHAR(10))
RETURNS (I INTEGER, LABEL VARCHAR(40))
AS
DECLARE VARIABLE TOTAL INTEGER = 0;
BEGIN
  I = 0;
  WHILE (I < N) DO
  BEGIN
    I = I + 1;
    TOTAL = TOTAL + I;
    IF (MOD(I, 2) = 0) THEN LABEL = PREFIX || ' even';
    ELSE LABEL = PREFIX || ' odd';
    INSERT INTO AUDIT_T VALUES (:I, :LABEL);
    SUSPEND;
  END
END^
CREATE FUNCTION F_RATIO (A INTEGER, B INTEGER) RETURNS INTEGER
AS
DECLARE VARIABLE Q INTEGER;
BEGIN
  Q = A * 10;
  Q = Q / B;
  RETURN Q;
END^
SET TERM ; ^
`;

test.beforeAll(async ({ browser, request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  const path = `${FB.dir}/fbadmin_e2edbg_${Date.now().toString(36)}.fdb`;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, { data: { name: DB_LABEL, path } });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  const r = await (await request.post(`/api/servers/${serverId}/databases/${dbId}/execute`, { data: { sql: SCHEMA } })).json();
  expect(r.error, JSON.stringify(r.error)).toBeNull();
  page = await (await browser.newContext()).newPage();
  await page.goto("/");
  await expand(page, SERVER);
  await expand(page, DB_LABEL);
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  if (!serverId) return;
  for (const tc of await (await request.get(`/api/servers/${serverId}/databases/${dbId}/debug/tests`)).json()) {
    await request.delete(`/api/servers/${serverId}/databases/${dbId}/debug/tests/${tc.id}`);
  }
  await request.post(`/api/servers/${serverId}/databases/${dbId}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

const pane = () => page.locator(".tab-pane:not(.hidden)");

async function audit(what: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
  expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), what).toEqual([]);
}

async function menu(rowLabel: string, item: string) {
  await treeRow(page, rowLabel).click({ button: "right" });
  const ctx = page.locator(".ctx").first();
  await expect(ctx).toBeVisible();
  const target = page.locator(".ctx button[role=menuitem]", { hasText: item }).first();
  if (!(await target.isVisible())) {
    for (const sub of await ctx.locator(":scope > .ctx-sub > button").all()) {
      await sub.click();
      if (await target.isVisible()) break;
    }
  }
  await target.click();
}

test("trace a procedure and step through the recorded steps", async () => {
  await expand(page, "Procedures");
  await menu("P_TRACE", "Debug / trace…");
  await expect(pane().locator(".dbg-note")).toContainText("rolled back");
  await expect(pane().locator(".dbg-src")).toContainText("TOTAL = TOTAL + I;");
  await pane().getByLabel("N", { exact: true }).fill("3");
  await pane().getByLabel("PREFIX", { exact: true }).fill("n");
  await pane().locator("button", { hasText: "Run trace" }).click();
  const result = pane().getByRole("table", { name: "Output rows" });
  await expect(result).toContainText("n odd");
  await expect(result.locator("tbody tr")).toHaveCount(3);
  await expect(pane().locator(".toolbar")).toContainText(/Step 1 of \d+/);
  // step forward: the current line moves, I becomes 0
  await pane().getByRole("button", { name: "Step forward" }).click();
  await expect(pane().locator(".toolbar")).toContainText("Step 2 of");
  const vars = pane().getByRole("table", { name: "Variables" });
  await expect(vars.locator("tr", { hasText: "TOTAL" })).toContainText("0");
  await pane().getByRole("button", { name: "Last step" }).click();
  await expect(vars.locator("tr", { hasText: "TOTAL" })).toContainText("6");
  await expect(pane().locator(".dbg-src .ln.cur")).toContainText("END");
  // click a line number: jump to the next step on that line
  await pane().getByRole("button", { name: "Go to the next step at line 7" }).click();
  await expect(pane().locator(".dbg-src .ln.cur")).toContainText("TOTAL = TOTAL + I;");
  // keyboard navigation in the step list
  await pane().getByRole("region", { name: "Recorded steps (arrow keys move)" }).focus();
  const before = await pane().locator(".toolbar").textContent();
  await page.keyboard.press("ArrowDown");
  await expect(pane().locator(".toolbar")).not.toHaveText(before ?? "");
  await audit("trace view");
  // the INSERTs were rolled back
  const r = await (await page.request.post(`/api/servers/${serverId}/databases/${dbId}/execute`, {
    data: { sql: "SELECT COUNT(*) FROM AUDIT_T" } })).json();
  expect(r.results[0].rows).toEqual([[0]]);
});

test("save the trace as a test and run it", async () => {
  await pane().locator("button", { hasText: "Save as test…" }).click();
  await expect(pane().getByLabel("Name", { exact: true })).toHaveValue("N=3, PREFIX=n");
  await expect(pane().getByLabel("Expected rows (JSON)")).toHaveValue(/n odd/);
  await audit("test form");
  await pane().locator("button", { hasText: "Save test" }).click();
  const row = pane().getByRole("table", { name: "Tests" }).locator("tr", { hasText: "N=3, PREFIX=n" });
  await expect(row).toBeVisible();
  await pane().locator("button", { hasText: "Run tests of P_TRACE" }).click();
  await expect(row.locator(".pill")).toHaveText("pass");
  // a failing test shows the difference
  await pane().locator("button", { hasText: "New test" }).click();
  await pane().getByLabel("Name", { exact: true }).fill("wrong");
  await pane().locator(".dbg-tests").getByLabel("N", { exact: true }).fill("1");
  await pane().locator(".dbg-tests").getByLabel("PREFIX", { exact: true }).fill("x");
  await pane().getByLabel("Expected rows (JSON)").fill('[[1, "x even"]]');
  await pane().locator("button", { hasText: "Save test" }).click();
  await pane().locator("button", { hasText: "Run all tests of this database" }).click();
  const bad = pane().getByRole("table", { name: "Tests" }).locator("tr", { hasText: "wrong" });
  await expect(bad.locator(".pill")).toHaveText("fail");
  await expect(bad).toContainText("expected 'x even', got 'x odd'");
  await expect(pane()).toContainText("1 passed");
  await audit("tests view");
});

test("function error is mapped to its source line", async () => {
  await expand(page, "Functions");
  await menu("F_RATIO", "Debug / trace…");
  await pane().getByLabel("A", { exact: true }).fill("7");
  await pane().getByLabel("B", { exact: true }).fill("0");
  await pane().locator("button", { hasText: "Run trace" }).click();
  await expect(pane().locator(".dbg-error")).toContainText("At source line 4");
  await expect(pane().locator(".dbg-src .ln.errline")).toContainText("Q = Q / B;");
  await pane().getByLabel("B", { exact: true }).fill("2");
  await pane().locator("button", { hasText: "Run trace" }).click();
  await expect(pane()).toContainText("Returned: 35");
  await audit("function trace");
});

test("trace an EXECUTE BLOCK and profile it", async () => {
  await menu(DB_LABEL, "Debug EXECUTE BLOCK…");
  await expect(pane().locator(".dbg-editor")).toBeVisible();
  await pane().locator("button", { hasText: "Run trace" }).click();
  await pane().getByLabel("N", { exact: true }).fill("4");
  await pane().locator("button", { hasText: "Run trace" }).click();
  await expect(pane().getByRole("table", { name: "Output rows" }).locator("tbody tr")).toHaveCount(4);
  await expect(pane().getByRole("table", { name: "Output rows" })).toContainText("16");
  const version = (await (await page.request.get(`/api/servers/${serverId}/databases/${dbId}/debug/routine?otype=functions&name=F_RATIO`)).json()).engine_version as string;
  if (Number(version.split(".")[0]) >= 5) {
    await pane().locator("button", { hasText: "Profile" }).click();
    await expect(pane().locator(".dbg-src.prof")).toBeVisible();
    await expect(pane().locator(".dbg-src .ln", { hasText: "SQUARE = I * I;" }).locator(".c")).toHaveText("4");
  } else {
    await expect(pane()).toContainText("Per-line timings (Profile) need Firebird 5");
  }
  await audit("execute block trace");
});

test("a long-running test run can be cancelled", async () => {
  const base = `/api/servers/${serverId}/databases/${dbId}`;
  const spin = "CREATE PROCEDURE P_SPIN RETURNS (N BIGINT) AS DECLARE I BIGINT = 0; BEGIN "
    + "WHILE (I < 4000000000) DO I = I + 1; N = I; SUSPEND; END";
  const r = await (await page.request.post(`${base}/execute`, { data: { sql: spin } })).json();
  expect(r.error, JSON.stringify(r.error)).toBeNull();
  const tc = await page.request.post(`${base}/debug/tests`, { data: { otype: "procedures", routine: "P_SPIN", name: "spin" } });
  expect(tc.status(), await tc.text()).toBe(201);
  await menu("P_TRACE", "Debug / trace…");
  await pane().getByRole("tab", { name: "Tests" }).click();
  const runAll = pane().locator("button", { hasText: "Run all tests of this database" });
  await runAll.click();
  const cancel = pane().getByRole("button", { name: "Cancel", exact: true });
  await expect(cancel).toBeVisible();
  await audit("test run in progress");
  await cancel.click();
  await expect(page.locator(".toast", { hasText: "The run was cancelled and rolled back" })).toBeVisible({ timeout: 30_000 });
  await expect(runAll).toBeEnabled();
  await expect(cancel).toBeHidden();
});
