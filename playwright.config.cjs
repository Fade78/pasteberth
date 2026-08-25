const { defineConfig, devices } = require("@playwright/test");

const browserName = process.env.E2E_BROWSER || "chromium";
const device = browserName === "firefox"
  ? devices["Desktop Firefox"]
  : devices["Desktop Chrome"];

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
    baseURL: "http://127.0.0.1:8876",
    headless: true,
    permissions: ["clipboard-read", "clipboard-write"],
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    video: "retain-on-failure",
  },
  webServer: {
    command: "python3 -m tests.browser_server",
    url: "http://127.0.0.1:8876/api/health",
    timeout: 120_000,
    reuseExistingServer: false,
  },
});
