import { defineConfig, devices } from "@playwright/test";
import path from "node:path";

const testArtifactsDirectory = path.resolve("test-results");

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["line"], ["html", { open: "never" }]] : "line",
  use: {
    baseURL: "http://localhost:3000",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: [
    {
      command: "uv run uvicorn app.main:app --host 127.0.0.1 --port 8000",
      cwd: "../backend",
      env: {
        EZTRIP_PLANNING_CHECKPOINT_DIR: path.join(
          testArtifactsDirectory,
          "planning-task-checkpoints",
        ),
        EZTRIP_PLANNING_TASK_STORE_PATH: path.join(
          testArtifactsDirectory,
          "planning-task-store.sqlite3",
        ),
      },
      url: "http://127.0.0.1:8000/api/health",
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
    },
    {
      command: "pnpm dev",
      cwd: ".",
      env: { NEXT_PUBLIC_API_BASE_URL: "http://localhost:8000" },
      url: "http://localhost:3000",
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
    },
  ],
});
