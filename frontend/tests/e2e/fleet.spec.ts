import { expect, test } from '@playwright/test'

// Fleet-page smoke — Vite on :5195; API mocked so the run is hermetic.
// Run with `just e2e` (or `cd frontend && npx playwright test`).

const sampleProjects = {
  projects: [
    {
      id: 'webapp',
      path: '/Users/dev/webapp',
      tier: 'gated',
      enabled: true,
      provider: 'grok_cli',
      model: 'grok-4.6',
      registered_at: '2026-08-19T00:00:00Z',
      current: {
        task_id: 'CBN-041',
        status: 'in-progress',
        reason: 'open tasknote',
        shortname: 'status-ui',
        plan_model: 'medium',
      },
      last_run: {
        task_id: 'CBN-040',
        outcome: 'closed',
        closed_at: '2026-08-19T01:00:00Z',
        summary: 'API landed.',
      },
    },
  ],
}

test('fleet page renders project table', async ({ page }) => {
  await page.route('**/api/projects', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(sampleProjects),
    })
  })

  await page.goto('/')

  await expect(page.getByRole('link', { name: 'Caobunga' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Fleet' })).toBeVisible()
  await expect(page.getByRole('table')).toBeVisible()
  await expect(page.getByRole('link', { name: 'webapp' })).toBeVisible()
})
