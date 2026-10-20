import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { expand, FB, setEditor, treeRow } from "./fixtures";

/**
 * Milestone 2.0: charts from Query Tool results (Grid | Chart view): chart types, columns,
 * grouping, "load all rows", keyboard tooltips, SVG / PNG export, axe in all three themes.
 */
test.describe.configure({ mode: "serial" });

const SERVER = "E2E Charts";
const SHOTS = process.env.E2E_SHOTS; // directory for screenshots (optional)
let page: Page;
let serverId = "";
let dbId = "";

const DATA = `
CREATE TABLE SALES (ID INTEGER NOT NULL PRIMARY KEY, REGION VARCHAR(60), PRODUCT VARCHAR(20), SOLD_ON DATE,
  AMOUNT NUMERIC(12,2), QTY INTEGER);
COMMIT;
EXECUTE BLOCK AS DECLARE i INTEGER = 0; BEGIN
  WHILE (i < 250) DO BEGIN
    INSERT INTO SALES VALUES (:i,
      CASE MOD(:i, 7) WHEN 0 THEN 'North' WHEN 1 THEN 'South' WHEN 2 THEN 'East' WHEN 3 THEN 'West'
        WHEN 4 THEN 'A region with a really very long name indeed' WHEN 5 THEN NULL ELSE 'Centre' END,
      CASE MOD(:i, 3) WHEN 0 THEN 'Apples' WHEN 1 THEN 'Pears' ELSE 'Plums' END,
      DATEADD(MOD(:i, 40) DAY TO DATE '2024-03-01'),
      CASE WHEN MOD(:i, 11) = 0 THEN -15 ELSE 10 + MOD(:i * 37, 90) END,
      MOD(:i, 9) + 1);
    i = i + 1;
  END
END;
`;

test.beforeAll(async ({ browser, request }) => {
  serverId = (await (await request.post("/api/servers", {
    data: { name: SERVER, host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json()).id;
  const db = await request.post(`/api/servers/${serverId}/databases/create`, {
    data: { name: "charts", path: `${FB.dir}/fbadmin_e2ech_${Date.now().toString(36)}.fdb` },
  });
  expect(db.status(), await db.text()).toBe(201);
  dbId = (await db.json()).id;
  const r = await (await request.post(`/api/servers/${serverId}/databases/${dbId}/execute`, { data: { sql: DATA } })).json();
  expect(r.error, JSON.stringify(r.error)).toBeNull();
  page = await (await browser.newContext({ acceptDownloads: true })).newPage();
  await page.goto("/");
});

test.afterAll(async ({ request }) => {
  await page?.context().close();
  if (!serverId) return;
  await request.post(`/api/servers/${serverId}/databases/${dbId}/drop`);
  await request.delete(`/api/servers/${serverId}`);
});

const qt = () => page.locator(".tab-pane:not(.hidden) .qt");
const chart = () => qt().locator(".qchart");

async function audit(what: string) {
  for (const theme of ["light", "dark", "contrast"]) {
    await page.evaluate((th) => { document.documentElement.dataset.theme = th; }, theme);
    const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
    const summary = r.violations.map((v) => `${v.id} (${v.impact}): ${v.nodes.slice(0, 3).map((n) => n.target.join(" ")).join(" | ")}`);
    expect(summary, `${what} (${theme}): axe violations`).toEqual([]);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/chart-${what.replace(/\W+/g, "-")}-${theme}.png` });
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
}

test("bar chart of a result, load all rows", async () => {
  await expand(page, SERVER);
  await treeRow(page, "charts").click();
  await page.locator(".topbar button", { hasText: "Query Tool" }).click();
  await qt().getByLabel("Rows").selectOption("100");
  await setEditor(page, qt(), "SELECT REGION, AMOUNT, QTY, SOLD_ON, PRODUCT FROM SALES ORDER BY ID");
  await qt().locator("button", { hasText: "Execute" }).click();
  await expect(qt().locator(".qt-out table")).toContainText("North");
  await qt().getByRole("button", { name: "Chart" }).click();
  const svg = chart().locator("svg[role=img]");
  await expect(svg).toHaveAttribute("aria-label", "Bar chart of AMOUNT, QTY by REGION");
  await expect(chart()).toContainText("Charting the 100 rows fetched so far");
  // NULL region is its own category, the long label is shortened on the axis
  await expect(svg).toContainText("(null)");
  await expect(svg).toContainText("A region with a r…");
  await chart().getByRole("button", { name: /Load all rows/ }).click();
  await expect(chart()).toContainText("Charting all 250 rows.");
  // the grid got the rows too
  await qt().getByRole("button", { name: "Grid" }).click();
  await expect(qt().locator(".qt-out .toolbar .muted").first()).toHaveText("250 rows");
  await qt().getByRole("button", { name: "Chart" }).click();
  await audit("bar");
});

test("keyboard: arrow keys show the tooltip of each category", async () => {
  const svg = chart().locator("svg[role=img]");
  await svg.focus();
  await page.keyboard.press("ArrowRight");
  const tip = chart().locator(".qchart-tip");
  await expect(tip).toContainText("North");
  await expect(tip).toContainText("AMOUNT");
  await page.keyboard.press("End");
  await expect(tip).not.toContainText("North");
  await page.keyboard.press("Escape");
  await expect(tip).toHaveCount(0);
});

test("data table, sorting, top N and grouping into series", async () => {
  await chart().getByRole("combobox", { name: "Sort", exact: true }).selectOption("value-desc");
  await chart().getByRole("spinbutton", { name: "Top", exact: true }).fill("3");
  await chart().getByRole("button", { name: "Data table" }).click();
  const table = chart().locator(".qchart-table table");
  await expect(table.locator("tbody tr")).toHaveCount(3);
  await expect(table.locator("tbody tr").last()).toContainText("Other");
  await expect(chart()).toContainText("smaller categories are combined into “Other”");
  await chart().getByRole("spinbutton", { name: "Top", exact: true }).fill("");
  await chart().getByRole("combobox", { name: "Series from", exact: true }).selectOption({ label: "PRODUCT" });
  await expect(table.locator("thead")).toContainText("Apples");
  await expect(table.locator("thead")).toContainText("Plums");
  await chart().getByRole("combobox", { name: "Chart", exact: true }).selectOption("stacked");
  await expect(chart().locator("svg[role=img]")).toHaveAttribute("aria-label", "Stacked bar chart of AMOUNT by REGION");
  await audit("stacked");
  await chart().getByRole("combobox", { name: "Series from", exact: true }).selectOption({ label: "value columns" });
  await chart().getByRole("button", { name: "Data table" }).click();
});

test("line over dates, pie, scatter", async () => {
  await chart().getByRole("combobox", { name: "Chart", exact: true }).selectOption("line");
  await chart().getByRole("combobox", { name: "Category", exact: true }).selectOption({ label: "SOLD_ON" });
  const svg = chart().locator("svg[role=img]");
  await expect(svg).toHaveAttribute("aria-label", "Line chart of AMOUNT, QTY by SOLD_ON");
  await expect(svg.locator("path[stroke-linejoin=round]")).toHaveCount(2);
  await expect(svg).toContainText("Mar"); // time axis labels
  await audit("line");
  await chart().getByRole("combobox", { name: "Chart", exact: true }).selectOption("donut");
  await chart().getByRole("combobox", { name: "Category", exact: true }).selectOption({ label: "PRODUCT" });
  await expect(svg).toHaveAttribute("aria-label", "Donut chart of AMOUNT, QTY by PRODUCT");
  await expect(svg).toContainText("Apples");
  await expect(svg).toContainText("%");
  await audit("donut");
  await chart().getByRole("combobox", { name: "Chart", exact: true }).selectOption("scatter");
  await chart().getByRole("combobox", { name: "X axis", exact: true }).selectOption({ label: "QTY" });
  await chart().locator("summary", { hasText: "Values" }).click();
  await chart().getByRole("checkbox", { name: "QTY" }).uncheck();
  await chart().locator("summary", { hasText: "Values" }).click();
  await expect(svg).toHaveAttribute("aria-label", "Scatter chart of AMOUNT by QTY");
  await expect(svg.locator("circle")).not.toHaveCount(0);
});

test("horizontal bars keep more of long labels; negative values extend left", async () => {
  await chart().getByRole("combobox", { name: "Chart", exact: true }).selectOption("hbar");
  await chart().getByRole("combobox", { name: "Category", exact: true }).selectOption({ label: "REGION" });
  await chart().getByRole("combobox", { name: "Combine", exact: true }).selectOption("min");
  const svg = chart().locator("svg[role=img]");
  await expect(svg).toHaveAttribute("aria-label", "Horizontal bar chart of AMOUNT by REGION");
  await expect(svg).toContainText("A region with a really ve…");
  await expect(chart().getByRole("spinbutton", { name: "Top", exact: true })).toHaveValue(""); // the pie's top 8 is not carried over
  await expect(svg.locator("text", { hasText: /^-\d+$/ }).first()).toBeVisible(); // the axis reaches below zero
  await audit("hbar");
  await chart().getByRole("combobox", { name: "Combine", exact: true }).selectOption("sum");
});

test("export SVG and PNG, copy TSV", async () => {
  await chart().getByRole("combobox", { name: "Chart", exact: true }).selectOption("bar");
  await chart().getByRole("combobox", { name: "Category", exact: true }).selectOption({ label: "REGION" });
  const [svgDl] = await Promise.all([page.waitForEvent("download"), chart().getByRole("button", { name: "SVG" }).click()]);
  expect(svgDl.suggestedFilename()).toBe("chart-bar.svg");
  const svgText = readFileSync(await svgDl.path());
  const s = svgText.toString("utf8");
  expect(s).toContain("<svg");
  expect(s).toContain('xmlns="http://www.w3.org/2000/svg"');
  expect(s).toContain("<title>Bar chart of AMOUNT by REGION</title>");
  expect(s).not.toContain("var(--");
  const [pngDl] = await Promise.all([page.waitForEvent("download"), chart().getByRole("button", { name: "PNG" }).click()]);
  expect(pngDl.suggestedFilename()).toBe("chart-bar.png");
  const png = readFileSync(await pngDl.path());
  expect(png.subarray(0, 8).toString("hex")).toBe("89504e470d0a1a0a");
  expect(png.length).toBeGreaterThan(2000);
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await chart().getByRole("button", { name: "TSV" }).click();
  const tsv = await page.evaluate(() => navigator.clipboard.readText());
  expect(tsv.split("\n")[0]).toBe("REGION\tAMOUNT");
  expect(tsv).toContain("North\t");
});
