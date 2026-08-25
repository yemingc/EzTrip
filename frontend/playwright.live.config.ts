import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/live",
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  reporter: "line",
  timeout: 210_000,
  expect: { timeout: 20_000 },
  use: {
    baseURL: "http://localhost:3000",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium-live",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: [
    {
      command: "uv run uvicorn app.main:app --host 127.0.0.1 --port 8000",
      cwd: "../backend",
      url: "http://127.0.0.1:8000/api/health",
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: "pnpm dev",
      cwd: ".",
      env: { NEXT_PUBLIC_API_BASE_URL: "http://localhost:8000" },
      url: "http://localhost:3000",
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
});
