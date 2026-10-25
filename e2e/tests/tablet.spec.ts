import AxeBuilder from "@axe-core/playwright";
import { devices, expect, test, type CDPSession, type Locator, type Page } from "@playwright/test";
import { FB, treeRow } from "./fixtures";

/**
 * Milestone 2.1 — tablet comfort, on an emulated iPad with real touch input (CDP touch events):
 * the ERD pans, moves tables and pinch-zooms with fingers; the query builder drags boxes by touch,
 * pinch-zooms and joins columns by tapping (HTML drag and drop does not work with touch); the table
 * designer's grids scroll sideways instead of squeezing their fields.
 */
test.describe.configure({ mode: "serial" });

const SERVER = "E2E Tablet";
const DB = "tabletdb";
let serverId = "";
let dbId = "";
let page: Page;
let cdp: CDPSession;

const DDL = `
CREATE TABLE AUTHOR (ID INTEGER NOT NULL PRIMARY KEY, NAME VARCHAR(40), COUNTRY VARCHAR(30));
CREATE TABLE BOOK (ID INTEGER NOT NULL PRIMARY KEY, AUTHOR_ID INTEGER REFERENCES AUTHOR (ID), TITLE VARCHAR(80));
CREATE TABLE LEFT_T (ID INTEGER NOT NULL PRIMARY KEY, CODE VARCHAR(10));
CREATE TABLE RIGHT_T (ID INTEGER NOT NULL PRIMARY KEY, CODE VARCHAR(10));
`;

test.beforeAll(async ({ browser, request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, {
    data: { name: DB, path: `${FB.dir}/fbadmin_e2et_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  const r = await (await request.post(`/api/servers/${serverId}/databases/${dbId}/execute`, { data: { sql: DDL } })).json();
  expect(r.error, JSON.stringify(r.error)).toBeNull();
  const { defaultBrowserType: _ignored, ...ipad } = devices["iPad (gen 7) landscape"];
  page = await (await browser.newContext(ipad)).newPage();
  cdp = await page.context().newCDPSession(page);
  await page.goto("/");
  await treeRow(page, SERVER).locator(".twisty").click();
  await treeRow(page, DB).locator(".twisty").click();
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  if (!serverId) return;
  await request.post(`/api/servers/${serverId}/databases/${dbId}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

const pane = () => page.locator(".tab-pane:not(.hidden)");

/** Touch gestures with one or two fingers: lists of [x, y] per finger, from start to end. */
async function touch(paths: Array<Array<[number, number]>>, steps = 8) {
  const at = (i: number) => paths.map((p, id) => {
    const f = i / steps;
    return { x: p[0][0] + (p[1][0] - p[0][0]) * f, y: p[0][1] + (p[1][1] - p[0][1]) * f, id };
  });
  await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: at(0) });
  for (let i = 1; i <= steps; i++) await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: at(i) });
  await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
}

async function center(l: Locator): Promise<[number, number]> {
  const b = (await l.boundingBox())!;
  return [b.x + b.width / 2, b.y + b.height / 2];
}

async function openFromMenu(rowLabel: string, item: string) {
  const row = treeRow(page, rowLabel);
  await row.click();
  await row.locator(".tree-more").click();
  const ctx = page.locator(".ctx").first();
  await expect(ctx).toBeVisible();
  const target = page.getByRole("menuitem", { name: item, exact: true }).first();
  if (!(await target.isVisible())) { // grouped menus: look in the submenus
    for (const sub of await ctx.locator(":scope > .ctx-sub > button").all()) {
      await sub.click();
      if (await target.isVisible()) break;
    }
  }
  await target.click();
}

async function audit(what: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).exclude(".cm-editor").analyze();
  expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), what).toEqual([]);
}

test("ERD: drag a table with a finger and pinch to zoom", async () => {
  await openFromMenu(DB, "ERD diagram");
  const svg = pane().locator("svg.sch-erd-canvas");
  const author = svg.locator("g.erd-node[data-table=AUTHOR]");
  await expect(author).toBeVisible();
  await expect(pane().locator(".sch-erd-help")).toContainText("Pinch: zoom");
  // move AUTHOR by its header
  const before = await author.getAttribute("transform");
  const [hx, hy] = await center(author.locator("path").first());
  await touch([[[hx, hy], [hx + 120, hy + 60]]]);
  await expect.poll(() => author.getAttribute("transform")).not.toBe(before);
  // pinch out: the zoom level grows
  const zoomText = pane().locator(".toolbar .muted", { hasText: /^\d+%$/ });
  const z0 = parseInt((await zoomText.textContent())!, 10);
  const [cx, cy] = await center(svg);
  await touch([[[cx - 40, cy], [cx - 160, cy]], [[cx + 40, cy], [cx + 160, cy]]]);
  await expect.poll(async () => parseInt((await zoomText.textContent())!, 10)).toBeGreaterThan(z0 * 1.5);
  // one finger on the background pans
  const root = svg.locator("g.erd-root");
  const t0 = await root.getAttribute("transform");
  const box = (await svg.boundingBox())!;
  await touch([[[box.x + 30, box.y + box.height - 40], [box.x + 130, box.y + box.height - 90]]]);
  await expect.poll(() => root.getAttribute("transform")).not.toBe(t0);
  await audit("ERD on a tablet");
});

test("query builder: touch drag, pinch zoom and tap-to-join", async () => {
  await openFromMenu(DB, "Query builder");
  const qb = pane().locator(".qb");
  const picker = qb.getByRole("combobox", { name: "Add table", exact: true });
  for (const name of ["LEFT_T", "RIGHT_T"]) {
    await picker.selectOption(`table:${name}`);
    await qb.getByRole("button", { name: "Add", exact: true }).click();
  }
  await expect(qb.locator(".qb-box")).toHaveCount(2);
  await expect(qb.locator(".qb-join-label")).toHaveCount(0); // no foreign key between them
  // drag the second box by its header
  const box2 = qb.locator(".qb-box").nth(1);
  const left0 = await box2.evaluate((el) => (el as HTMLElement).style.left);
  const [hx, hy] = await center(box2.locator(".qb-box-head .qb-name"));
  await touch([[[hx, hy], [hx + 90, hy + 70]]]);
  await expect.poll(() => box2.evaluate((el) => (el as HTMLElement).style.left)).not.toBe(left0);
  // tap the join button of LEFT_T.CODE, then of RIGHT_T.CODE
  await qb.getByRole("button", { name: "Join on LT.CODE", exact: true }).click();
  await expect(qb.locator(".qb-join-hint")).toBeVisible();
  await audit("query builder join hint on a tablet");
  await qb.getByRole("button", { name: "Join on RT.CODE", exact: true }).click();
  await expect(qb.locator(".qb-join-label")).toHaveCount(1);
  await expect(qb.locator(".qb-join-hint")).toHaveCount(0);
  // pinch in: zoom out
  const zoom = qb.locator(".qb-zoom button", { hasText: /%$/ });
  await expect(zoom).toHaveText("100%");
  const [cx, cy] = await center(qb.locator(".qb-canvas"));
  await touch([[[cx - 150, cy], [cx - 40, cy]], [[cx + 150, cy], [cx + 40, cy]]]);
  await expect.poll(async () => parseInt((await zoom.textContent())!, 10)).toBeLessThan(80);
  await zoom.click();
  await expect(zoom).toHaveText("100%");
});

test("table designer: the column grid scrolls sideways with usable fields", async () => {
  await treeRow(page, "Tables").locator(".twisty").click();
  await openFromMenu("AUTHOR", "Table designer…");
  const dlg = page.getByRole("dialog");
  await dlg.getByRole("button", { name: /^Columns \(\d+\)$/ }).click();
  const grid = dlg.locator("table.designer").first();
  await expect(grid).toBeVisible();
  const name = grid.locator("tbody tr").first().locator("input.text").first();
  expect((await name.boundingBox())!.width).toBeGreaterThanOrEqual(129);
  const scrolls = await grid.evaluate((el) => el.scrollWidth > el.clientWidth || getComputedStyle(el).overflowX === "auto");
  expect(scrolls).toBe(true);
  const check = grid.locator("tbody tr").first().locator("input[type=checkbox]").first();
  expect((await check.boundingBox())!.width).toBeGreaterThanOrEqual(21);
  await audit("table designer on a tablet");
});
