import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 45_000,
  use: {
    baseURL: "http://127.0.0.1:3000",
    viewport: { width: 1280, height: 900 },
    trace: "retain-on-failure",
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
      ? {
          executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
          args: ["--no-sandbox", "--disable-dev-shm-usage"],
        }
      : {},
  },
  webServer: [
    {
      command: "python -m uvicorn browser_app:app --host 127.0.0.1 --port 8000",
      cwd: "..",
      env: { PYTHONPATH: "backend:backend/tests" },
      url: "http://127.0.0.1:8000/healthz",
      reuseExistingServer: false,
    },
    {
      command: "npm run dev -- --host 127.0.0.1 --port 3000 --strictPort",
      env: { VITE_API_URL: "http://127.0.0.1:8000" },
      url: "http://127.0.0.1:3000",
      reuseExistingServer: false,
    },
  ],
});
