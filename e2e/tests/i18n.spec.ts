import { expect, test } from "@playwright/test";

/** The UI follows the language preference (and the browser language by default). */
test("switches the UI language", async ({ browser }) => {
  for (const [lang, addServer, queryTool] of [
    ["de", "Server hinzufügen", "Abfragewerkzeug"],
    ["ro", "Adăugare server", "Query Tool"],
    ["ru", "Добавить сервер", "Редактор запросов"],
  ]) {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    await page.addInitScript((l) => localStorage.setItem("fbadmin.prefs", JSON.stringify({ language: l })), lang);
    await page.goto("/");
    await expect(page.locator(".topbar")).toContainText(addServer);
    await expect(page.locator(".topbar")).toContainText(queryTool);
    await expect(page.locator("html")).toHaveAttribute("lang", lang);
    await ctx.close();
  }
});

test("uses the browser language when no preference is set", async ({ browser }) => {
  const ctx = await browser.newContext({ locale: "fr-FR" });
  const page = await ctx.newPage();
  await page.goto("/");
  await expect(page.locator(".topbar")).toContainText("Outil de requête");
  await ctx.close();
});
