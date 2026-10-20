import AxeBuilder from "@axe-core/playwright";
import { devices, expect, request as pwRequest, test, type Browser, type BrowserContext, type Page } from "@playwright/test";
import { spawn, type ChildProcess } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { FB, SCHEMA, setEditor, treeRow } from "./fixtures";

/**
 * Phone and tablet layout (features/mobile): device emulation with touch. Phones land on the
 * overview, the Object Explorer is a drawer, long-press / "⋮" open the context menu as a bottom
 * sheet, dialogs are full-screen; tablets get a collapsible sidebar. Every step checks that the page
 * never scrolls sideways, and axe audits run in the light, dark and high-contrast themes (with the
 * WCAG 2.2 target-size rule). The last test signs in to a server-mode instance on a phone.
 */
test.describe.configure({ mode: "serial" });

const SERVER = "E2E Mobile";
const DB = "mobiledb";
let serverId = "";
let dbId = "";

// device descriptors without defaultBrowserType (the suite runs in Chromium)
function device(name: string) {
  const { defaultBrowserType: _ignored, ...d } = devices[name];
  return d;
}

test.beforeAll(async ({ request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, {
    data: { name: DB, path: `${FB.dir}/fbadmin_e2em_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  // a wide row, so the result grid has to scroll sideways
  const wide = "ALTER TABLE CUSTOMER ADD NOTE VARCHAR(200);\nUPDATE CUSTOMER SET NOTE = 'a fairly long note that makes this result grid much wider than a phone screen';";
  await request.post(`/api/servers/${serverId}/databases/${dbId}/execute`, { data: { sql: SCHEMA + wide } });
});

test.afterAll(async ({ request }) => {
  if (!serverId) return;
  await request.post(`/api/servers/${serverId}/databases/${dbId}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

async function newPage(browser: Browser, name: string, landscape = false): Promise<Page> {
  const ctx: BrowserContext = await browser.newContext(device(landscape ? `${name} landscape` : name));
  return ctx.newPage();
}

/** The page itself never scrolls sideways; wide content scrolls inside its own box. */
async function noSideScroll(page: Page, what: string) {
  const [sw, w] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
  expect(sw, `${what}: page wider than the screen`).toBeLessThanOrEqual(w);
}

async function audit(page: Page, what: string, targetSize = true) {
  for (const theme of ["", "dark", "contrast"]) {
    await page.evaluate((th) => { if (th) document.documentElement.dataset.theme = th; else delete document.documentElement.dataset.theme; }, theme);
    const tags = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];
    // wcag22aa brings in target-size (24 × 24 px)
    const r = await new AxeBuilder({ page }).withTags(targetSize ? [...tags, "wcag22aa"] : tags).exclude(".cm-editor").analyze();
    const v = r.violations.map((x) => `${x.id}: ${x.nodes.slice(0, 4).map((n) => n.target.join(" ")).join(" | ")}`);
    expect(v, `${what} (${theme || "light"})`).toEqual([]);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
}

/** Touch and hold (CDP touch events: Playwright has no long-press). */
async function longPress(page: Page, x: number, y: number, ms = 800) {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x, y }] });
  await page.waitForTimeout(ms);
  await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  await cdp.detach();
}

const pane = (page: Page) => page.locator(".tab-pane:not(.hidden)");
const activeTab = (page: Page) => page.getByRole("tablist", { name: "Panels" }).getByRole("tab", { selected: true });

async function openDrawer(page: Page) {
  const toggle = page.locator(".nav-toggle");
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
  await expect(page.locator("nav.sidebar.drawer.open")).toBeVisible();
}

test.describe("phone", () => {
  let page: Page;
  test.beforeAll(async ({ browser }) => { page = await newPage(browser, "Pixel 7"); });
  test.afterAll(async () => { await page?.context().close(); });

  test("lands on the overview, readable without sideways scrolling", async () => {
    await page.goto("/");
    await expect(activeTab(page)).toHaveText("Overview");
    const card = pane(page).locator(".ov-server", { hasText: SERVER });
    await expect(card.locator("tr", { hasText: DB }).locator(".ov-badge")).toHaveText(/OK/);
    // the database row is a card: field names come from data-label
    await expect(card.locator("td[data-label='Attachments']")).toBeVisible();
    await noSideScroll(page, "overview");
    // the Object Explorer is closed; the topbar keeps the overview and the menus
    await expect(page.locator("nav.sidebar")).toBeHidden();
    await expect(page.locator(".topbar button", { hasText: "Add server" })).toHaveCount(0);
    await audit(page, "overview (phone)");
  });

  test("Object Explorer drawer: focus, Escape, tap outside, swipe", async () => {
    const toggle = page.locator(".nav-toggle");
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    const nav = page.locator("nav.sidebar.drawer.open");
    await expect(nav).toBeVisible();
    await expect.poll(() => page.evaluate(() => !!document.activeElement?.closest("nav.sidebar"))).toBe(true);
    // the background is inert while the drawer is open
    expect(await page.evaluate(() => (document.querySelector(".workspace") as HTMLElement).inert)).toBe(true);
    await noSideScroll(page, "drawer");
    await audit(page, "drawer (phone)");
    await page.keyboard.press("Escape");
    await expect(nav).toBeHidden();
    await expect(toggle).toBeFocused();
    // tap outside closes it
    await toggle.click();
    await page.locator(".drawer-back").click({ position: { x: page.viewportSize()!.width - 12, y: 300 } });
    await expect(page.locator("nav.sidebar.drawer.open")).toHaveCount(0);
    // so does a swipe to the left
    await toggle.click();
    await expect(nav).toBeVisible();
    const cdp = await page.context().newCDPSession(page);
    await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: 250, y: 400 }] });
    for (const x of [200, 150, 100, 60]) await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x, y: 405 }] });
    await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    await cdp.detach();
    await expect(page.locator("nav.sidebar.drawer.open")).toHaveCount(0);
  });

  test("opening a database from the drawer shows its dashboard", async () => {
    await openDrawer(page);
    await treeRow(page, SERVER).locator(".twisty").click();
    await treeRow(page, DB).click();
    await expect(page.locator("nav.sidebar.drawer.open")).toHaveCount(0);
    await expect(activeTab(page)).toHaveText("Dashboard");
    await expect(pane(page).locator("h2", { hasText: DB })).toBeVisible();
    await noSideScroll(page, "database dashboard");
    await audit(page, "database dashboard (phone)");
  });

  test("attachments and statements (Monitor)", async () => {
    await pane(page).locator("button", { hasText: "Monitor" }).first().click();
    await expect(pane(page).locator("h3", { hasText: "Attachments" })).toBeVisible();
    const grid = pane(page).locator(".grid-wrap").first();
    await expect(grid.locator("tbody tr").first()).toBeVisible();
    // the table scrolls inside its own box
    expect(await grid.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);
    await noSideScroll(page, "monitor");
    await audit(page, "monitor (phone)");
  });

  test("long-press on a tree row opens the context menu as a bottom sheet", async () => {
    await openDrawer(page);
    await treeRow(page, DB).locator(".twisty").click();
    await treeRow(page, "Tables").locator(".twisty").click();
    const row = treeRow(page, "CUSTOMER");
    await expect(row).toBeVisible();
    const box = (await row.boundingBox())!;
    await longPress(page, box.x + 90, box.y + box.height / 2);
    const menu = page.locator(".ctx.sheet[role=menu]");
    await expect(menu).toBeVisible();
    await expect(row).toHaveAttribute("aria-selected", "true");
    // a sheet along the bottom edge, full width
    const mb = (await menu.boundingBox())!;
    const vp = page.viewportSize()!;
    expect(Math.round(mb.width)).toBe(vp.width);
    expect(Math.round(mb.y + mb.height)).toBe(vp.height);
    await expect(menu.getByRole("menuitem", { name: "View/Edit data" })).toBeVisible();
    await noSideScroll(page, "context menu");
    await audit(page, "context menu sheet (phone)");
    // tapping outside closes it and leaves the drawer open
    await page.locator(".ctx-back").click({ position: { x: 20, y: 20 } });
    await expect(menu).toHaveCount(0);
    await expect(page.locator("nav.sidebar.drawer.open")).toBeVisible();
    // the selected row has a "⋮" button for the same menu
    await row.locator(".tree-more").click();
    await expect(menu).toBeVisible();
    await menu.getByRole("menuitem", { name: "Query Tool" }).click();
    // opening a tab closes the drawer
    await expect(page.locator("nav.sidebar.drawer.open")).toHaveCount(0);
    await expect(pane(page).locator(".qt")).toBeVisible();
  });

  test("a quick SELECT in the Query Tool; the grid scrolls inside", async () => {
    const qt = pane(page).locator(".qt");
    await setEditor(page, qt, "SELECT * FROM CUSTOMER");
    await qt.locator("button", { hasText: "Execute" }).click();
    const out = qt.locator(".qt-out");
    await expect(out.locator("table")).toContainText("Bucharest");
    // the box around the result table scrolls sideways (the page does not)
    const scrolled = await out.evaluate((el) => {
      const scroller = [el, ...Array.from(el.querySelectorAll<HTMLElement>("*"))]
        .find((e) => e.scrollWidth > e.clientWidth + 10 && ["auto", "scroll"].includes(getComputedStyle(e).overflowX));
      if (!scroller) return 0;
      scroller.scrollLeft = 200;
      return scroller.scrollLeft;
    });
    expect(scrolled).toBeGreaterThan(0);
    await noSideScroll(page, "query tool");
    await audit(page, "query tool (phone)");
  });

  test("topbar overflow menu and a dialog as a full-screen sheet", async () => {
    const more = page.locator(".topbar .tb-more");
    await more.click();
    const sheet = page.getByRole("dialog", { name: "Menu" });
    await expect(sheet).toBeVisible();
    await noSideScroll(page, "overflow menu");
    await audit(page, "overflow menu (phone)");
    await sheet.locator(".tb", { hasText: "Add server" }).click();
    await expect(sheet).toHaveCount(0);
    const dlg = page.getByRole("dialog", { name: "Register server" });
    await expect(dlg).toBeVisible();
    const b = (await dlg.boundingBox())!;
    const vp = page.viewportSize()!;
    expect([Math.round(b.x), Math.round(b.y), Math.round(b.width), Math.round(b.height)]).toEqual([0, 0, vp.width, vp.height]);
    // labels above their fields
    const label = dlg.locator("label.l", { hasText: "Host" });
    const input = dlg.locator("label.l:text-is('Host') + *");
    expect((await input.boundingBox())!.y).toBeGreaterThan((await label.boundingBox())!.y + 10);
    await noSideScroll(page, "dialog");
    await audit(page, "dialog (phone)");
    await dlg.getByRole("button", { name: "Close" }).click();
    await expect(dlg).toHaveCount(0);
  });
});

test.describe("small phone (360 px)", () => {
  test("overview, drawer and menus fit", async ({ browser }) => {
    const ctx = await browser.newContext({ ...device("Galaxy S9+"), viewport: { width: 360, height: 740 } });
    const page = await ctx.newPage();
    await page.goto("/");
    await expect(activeTab(page)).toHaveText("Overview");
    await expect(pane(page).locator(".ov-server", { hasText: SERVER })).toBeVisible();
    await noSideScroll(page, "overview at 360 px");
    await openDrawer(page);
    await noSideScroll(page, "drawer at 360 px");
    await page.keyboard.press("Escape");
    await page.locator(".topbar .tb-more").click();
    await expect(page.getByRole("dialog", { name: "Menu" })).toBeVisible();
    await noSideScroll(page, "menu at 360 px");
    await ctx.close();
  });

  test("iPhone 13: landing and drawer", async ({ browser }) => {
    const page = await newPage(browser, "iPhone 13");
    await page.goto("/");
    await expect(activeTab(page)).toHaveText("Overview");
    await openDrawer(page);
    await expect(treeRow(page, SERVER)).toBeVisible();
    await noSideScroll(page, "iPhone drawer");
    await page.context().close();
  });
});

for (const landscape of [false, true]) {
  test(`tablet ${landscape ? "landscape" : "portrait"}: collapsible sidebar, remembered`, async ({ browser }) => {
    const page = await newPage(browser, "iPad (gen 7)", landscape);
    await page.goto("/");
    await page.evaluate(() => localStorage.removeItem("fbadmin.sidebarCollapsed"));
    await page.reload();
    const nav = page.locator("nav.sidebar");
    await expect(nav).toBeVisible();
    // desktop default: no overview tab opened on start
    await expect(activeTab(page)).toHaveText("Dashboard");
    expect((await nav.boundingBox())!.width).toBeLessThanOrEqual(270);
    const toggle = page.locator(".nav-toggle");
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await noSideScroll(page, "tablet");
    await audit(page, `tablet ${landscape ? "landscape" : "portrait"}`);
    await toggle.click();
    await expect(nav).toBeHidden();
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await page.reload();
    await expect(page.locator(".nav-toggle")).toHaveAttribute("aria-expanded", "false");
    await expect(nav).toBeHidden();
    await page.locator(".nav-toggle").click();
    await expect(nav).toBeVisible();
    // the context menu stays a menu next to the row on tablets
    await treeRow(page, SERVER).click({ button: "right" });
    await expect(page.locator(".ctx[role=menu]")).toBeVisible();
    await expect(page.locator(".ctx.sheet")).toHaveCount(0);
    await page.keyboard.press("Escape");
    await page.context().close();
  });
}

test("web app manifest and icons are served", async ({ request }) => {
  const r = await request.get("/manifest.webmanifest");
  expect(r.status()).toBe(200);
  expect(r.headers()["content-type"]).toContain("application/manifest+json");
  const m = await r.json();
  expect(m.short_name).toBe("fbadmin");
  for (const icon of m.icons) {
    const i = await request.get(icon.src);
    expect(i.status(), icon.src).toBe(200);
  }
  const html = await (await request.get("/")).text();
  expect(html).toContain('rel="manifest"');
  expect(html).toContain("apple-mobile-web-app-capable");
});

test.describe("server mode on a phone", () => {
  // its own fbadmin in server mode, next to the one from access.spec.ts (E2E_PORT + 1000)
  const PORT = Number(process.env.E2E_MOBILE_SERVER_MODE_PORT ?? Number(process.env.E2E_PORT ?? 5106) + 1001);
  const BASE = `http://127.0.0.1:${PORT}`;
  const ADMIN = { user: "admin", password: "admin-password-mobile" };
  let proc: ChildProcess | null = null;
  let dataDir = "";

  test.beforeAll(async () => {
    dataDir = mkdtempSync(join(tmpdir(), "fbadmin-e2e-mob-"));
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
  });

  test.afterAll(async () => {
    proc?.kill();
    if (dataDir) rmSync(dataDir, { recursive: true, force: true });
  });

  test("sign in, then sign out from the overflow menu", async ({ browser }) => {
    const page = await newPage(browser, "Pixel 7");
    await page.goto(BASE + "/");
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
    await noSideScroll(page, "login page");
    await audit(page, "login page (phone)");
    await page.getByLabel("User name or e-mail").fill(ADMIN.user);
    await page.getByLabel("Password", { exact: true }).fill(ADMIN.password);
    await page.locator("button[type=submit]", { hasText: "Sign in" }).click();
    await expect(page.locator(".topbar .nav-toggle")).toBeVisible();
    await noSideScroll(page, "signed in");
    await page.locator(".topbar .tb-more").click();
    const sheet = page.getByRole("dialog", { name: "Menu" });
    // the user menu opens inside the sheet (a disclosure: the sheet stays open)
    await sheet.locator(".user-menu > button.tb").click();
    await expect(sheet.locator(".user-menu-pop")).toBeVisible();
    await noSideScroll(page, "user menu");
    await sheet.locator(".user-menu-pop button", { hasText: "Sign out" }).click();
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
    await page.context().close();
  });
});
