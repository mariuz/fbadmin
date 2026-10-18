import { defineConfig, devices } from "@playwright/test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

/**
 * End-to-end tests: a real browser against a real fbadmin (built UI + backend) and a real
 * Firebird server. See docs/DEVELOPMENT.md.
 *
 * Environment:
 *   E2E_PORT          port for the fbadmin server started by Playwright (default 5106)
 *   E2E_BASE_URL      test an already running fbadmin instead of starting one
 *   FBADMIN_PYTHON    Python with fbadmin's dependencies installed (default "python3")
 *   FB_HOST / FB_PORT / FB_PASSWORD / FB_DB_DIR   the Firebird server (see tests/fixtures.ts)
 *   PW_CHROMIUM_PATH  use this Chromium binary instead of Playwright's download
 */
const port = Number(process.env.E2E_PORT ?? 5106);
const baseURL = process.env.E2E_BASE_URL ?? `http://127.0.0.1:${port}`;
const dataDir = mkdtempSync(join(tmpdir(), "fbadmin-e2e-"));

export default defineConfig({
  testDir: "./tests",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1, // the specs share one fbadmin instance and one Firebird database
  retries: process.env.CI ? 1 : 0,
  forbidOnly: !!process.env.CI,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    viewport: { width: 1400, height: 900 },
    launchOptions: process.env.PW_CHROMIUM_PATH ? { executablePath: process.env.PW_CHROMIUM_PATH } : {},
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1400, height: 900 } } }],
  webServer: process.env.E2E_BASE_URL
    ? undefined
    : {
        command: `${process.env.FBADMIN_PYTHON ?? "python3"} -m fbadmin --no-browser --host 127.0.0.1 --port ${port} --log-level warning`,
        cwd: resolve(__dirname, "../backend"),
        url: `${baseURL}/api/health`,
        reuseExistingServer: false,
        timeout: 60_000,
        env: { FBADMIN_DATA_DIR: dataDir },
        stdout: "pipe",
        stderr: "pipe",
      },
});
