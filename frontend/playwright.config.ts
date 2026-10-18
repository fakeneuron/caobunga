import { defineConfig, devices } from '@playwright/test'

// Playwright e2e — specs live in tests/e2e/. Boots the Vite dev server first.
//
// PORT: this project is 5195 (registry in ~/.claude/CLAUDE.md). `baseURL`,
// `webServer.url`, and vite.config.ts `server.port` + `strictPort: true` must
// all name it. A mismatch fails silently — the webServer wait can only time
// out, and a repo shipped exactly that state for two months before anyone ran
// the suite (Fleet HQ NAT-186.2). caobunga's port already moved once
// (5193 → 5195, CBN-105), so this is a live hazard here, not a hypothetical.
//
// See Fleet HQ's docs/STACK-TENDENCIES.md §"End-to-end testing (Playwright)".
export default defineConfig({
  testDir: './tests/e2e',
  // Cap local runs at 2 — the fanless M3 Air thermal-throttles above that and
  // flakes; CI runs serial for determinism (NAT-186.3).
  workers: process.env.CI ? 1 : 2,
  // CI writes the html report the failure-artifact upload collects; locally
  // `list` is terse, and `html` would serve the report and block the terminal
  // on a failed run at a TTY (NAT-370).
  reporter: process.env.CI ? [['dot'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: 'http://127.0.0.1:5195',
    trace: 'on-first-retry',
  },
  webServer: {
    command: 'npm run dev',
    url: 'http://127.0.0.1:5195',
    reuseExistingServer: !process.env.CI,
    // Playwright SIGKILLs a webServer's process tree by default, orphaning its
    // Vite/node children. SIGTERM first lets them tear down (NAT-186.3).
    gracefulShutdown: { signal: 'SIGTERM', timeout: 5_000 },
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
})
