const { defineConfig, devices } = require("@playwright/test");

const browserName = process.env.E2E_BROWSER || "chromium";
const urlPrefix = process.env.PASTEBERTH_E2E_PREFIX || "";
const port = process.env.PASTEBERTH_E2E_PORT || "8876";
const baseURL = `http://127.0.0.1:${port}${urlPrefix}/`;
const healthPath = `${urlPrefix}/api/health`;
const device = browserName === "firefox"
  ? devices["Desktop Firefox"]
  : devices["Desktop Chrome"];
const permissions = browserName === "chromium"
  ? ["clipboard-read", "clipboard-write"]
  : [];

module.exports = defineConfig({
  testDir: "./tests/e2e",
  outputDir: "./test-results",
  timeout: 30_000,
  expect: { timeout: 8_000 },
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI
    ? [["line"], ["junit", { outputFile: "test-results/e2e-junit.xml" }]]
    : "list",
  use: {
    ...device,
    browserName,
    baseURL,
    headless: true,
    permissions,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    video: "retain-on-failure",
  },
  webServer: {
    command: "python3 -m tests.browser_server",
    url: `http://127.0.0.1:${port}${healthPath}`,
    timeout: 120_000,
    reuseExistingServer: false,
  },
});
