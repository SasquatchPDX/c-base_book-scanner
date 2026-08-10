import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/browser",
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? [["github"], ["line"]] : "line",
  use: {
    baseURL: process.env.BOOK_SCANNER_BROWSER_BASE_URL ?? "http://127.0.0.1:58080",
    trace: "retain-on-failure",
    ...devices["Desktop Chrome"],
  },
});
