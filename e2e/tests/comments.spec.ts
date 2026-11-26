import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { FB, expand, treeRow, shot } from "./fixtures";

/**
 * Roadmap 2.4: comments on objects and notebooks. A table's Comments tab holds a thread: add, edit, delete;
 * a notebook shows its comments below its cells. (backend/tests/test_comments.py covers permissions.)
 */
test.describe.configure({ mode: "serial" });

const SERVER = `E2E Comments ${Date.now().toString(36)}`;
let page: Page;
let serverId = "";
let dbId = "";
const base = () => `/api/servers/${serverId}/databases/${dbId}`;

test.beforeAll(async ({ browser, request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, {
    data: { name: "notesdb", path: `${FB.dir}/fbadmin_e2ecm_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  expect((await (await request.post(base() + "/execute", { data: { sql: "CREATE TABLE ORDERS (ID INTEGER NOT NULL PRIMARY KEY)" } })).json()).error).toBeNull();
  page = await (await browser.newContext()).newPage();
  await page.goto("/");
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  for (const n of (await (await request.get("/api/notebooks", { params: { database_id: dbId } })).json()).notebooks ?? []) {
    await request.delete(`/api/notebooks/${n.id}`);
  }
  await request.post(`${base()}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

async function audit(what: string) {
  for (const theme of ["light", "dark", "contrast"]) {
    await page.evaluate((th) => { document.documentElement.dataset.theme = th; }, theme);
    const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
    expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), `${what} (${theme})`).toEqual([]);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
}

test("a thread on a table", async () => {
  await expand(page, SERVER);
  await expand(page, "notesdb");
  await expand(page, "Tables");
  await treeRow(page, "ORDERS").click();
  await page.getByRole("tab", { name: "Comments" }).click();
  const thread = page.getByRole("region", { name: "Comments on ORDERS" });
  await expect(thread).toContainText("No comments yet");
  await thread.getByLabel("New comment").fill("Needed by the nightly report");
  await thread.getByRole("button", { name: "Comment", exact: true }).click();
  await expect(thread.locator(".cmt-text")).toHaveText(["Needed by the nightly report"]);
  await thread.getByLabel("New comment").fill("Partitioned by month");
  await thread.getByLabel("New comment").press("Control+Enter");
  await expect(thread.locator(".cmt-text")).toHaveCount(2);

  await thread.locator(".cmt-item").first().getByRole("button", { name: "Edit" }).click();
  await thread.getByLabel("Edit the comment").fill("Needed by the nightly and the weekly report");
  await thread.getByRole("button", { name: "Save" }).click();
  await expect(thread.locator(".cmt-text").first()).toHaveText("Needed by the nightly and the weekly report");
  await expect(thread.locator(".cmt-meta").first()).toContainText("edited");
  await audit("comments tab");
  await shot(page, "comments"); // docs/screenshots/comments.png

  page.once("dialog", (d) => d.accept());
  await thread.locator(".cmt-item").nth(1).getByRole("button", { name: "Delete" }).click();
  await expect(thread.locator(".cmt-text")).toHaveCount(1);
});

test("a notebook's comments", async () => {
  await treeRow(page, "notesdb").click({ button: "right" });
  const ctx = page.locator(".ctx").first();
  const target = page.locator(".ctx button[role=menuitem]", { hasText: "New notebook…" }).first();
  if (!(await target.isVisible())) {
    for (const sub of await ctx.locator(":scope > .ctx-sub > button").all()) {
      await sub.click();
      if (await target.isVisible()) break;
    }
  }
  await target.click();
  const thread = page.getByRole("region", { name: "Comments on this notebook" });
  await expect(thread).toContainText("No comments yet");
  await thread.getByLabel("New comment").fill("Run it after the month-end close");
  await thread.getByRole("button", { name: "Comment", exact: true }).click();
  await expect(page.locator(".nb-comments > summary")).toHaveText("Comments (1)");
});
