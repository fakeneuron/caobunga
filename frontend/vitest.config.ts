import { defineConfig, mergeConfig } from 'vitest/config'
import viteConfig from './vite.config.ts'

// Vitest 4 — merges the base Vite config (so the React plugin is reused for
// component tests) and adds the test layer: happy-dom env, jest-dom setup,
// v8 coverage. Unit tests live in tests/unit/ (Playwright owns tests/e2e/).
// See Fleet HQ's docs/CODING-STANDARDS.md §Testing.
//
// Coverage feeds the CI changed-line gate (docs/STACK-TENDENCIES.md §CI), so
// two keys below are load-bearing rather than cosmetic:
//   `include` — WITHOUT it the v8 provider reports only files a test actually
//     imported, so a brand-new untested module is absent from the report
//     entirely and diff-cover scores it as "no coverage information" instead of
//     0%. That is the gate's worst failure mode: a green run on the exact
//     change it exists to catch. Measured at Fleet HQ NAT-229.
//   `lcovonly` — the report format diff-cover reads. `text` keeps the summary
//     visible in the CI log.
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: 'happy-dom',
      setupFiles: ['./tests/setup.ts'],
      include: ['tests/unit/**/*.{test,spec}.{ts,tsx}'],
      coverage: {
        provider: 'v8',
        reportsDirectory: './coverage',
        reporter: ['text', 'lcovonly'],
        include: ['src/**/*.{ts,tsx}'],
      },
    },
  }),
)
