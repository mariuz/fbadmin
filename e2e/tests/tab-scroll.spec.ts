import { expect, test } from "@playwright/test";

/**
 * Issue #4: tabs opened on their own (not inside the maintenance tab) must scroll, so long forms stay reachable.
 * A short window makes the New schedule form taller than the pane.
 */
test.use({ viewport: { width: 1280, height: 520 } });

test("the New schedule form scrolls to its Save button", async ({ page }) => {
  await page.goto("/");
  await page.locator(".topbar button", { hasText: "Schedules" }).click();
  const pane = page.locator(".tab-pane:not(.hidden)");
  await pane.locator("button", { hasText: "New schedule" }).click();
  const save = pane.locator("button.btn.primary", { hasText: "Save" });
  await save.scrollIntoViewIfNeeded();
  await expect(save).toBeInViewport();
  // the scrolling element is inside the pane, and the pane itself still clips
  const scrolled = await pane.evaluate((el) => {
    const root = el.firstElementChild as HTMLElement;
    return { root: root.scrollTop, pane: el.scrollTop, overflow: root.scrollHeight > root.clientHeight };
  });
  expect(scrolled.overflow).toBe(true);
  expect(scrolled.root).toBeGreaterThan(0);
  expect(scrolled.pane).toBe(0);
});

test("other feature tabs scroll too", async ({ page }) => {
  await page.goto("/");
  for (const label of ["Alerts", "Library", "Overview"]) {
    await page.locator(".topbar button", { hasText: label }).click();
    const root = page.locator(".tab-pane:not(.hidden) > *").first();
    const box = await root.evaluate((el) => {
      const r = el.getBoundingClientRect();
      const pane = el.parentElement!.getBoundingClientRect();
      return { bottom: r.bottom, paneBottom: pane.bottom, overflowY: getComputedStyle(el).overflowY };
    });
    expect(box.bottom, label).toBeLessThanOrEqual(box.paneBottom + 1); // fits the pane: scrolls rather than clips
    expect(["auto", "scroll"], label).toContain(box.overflowY);
  }
});
