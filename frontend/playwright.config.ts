import { defineConfig, devices } from '@playwright/test';

import { frontendBaseUrl } from './e2e/env';

export default defineConfig({
	testDir: './e2e',
	globalSetup: './e2e/global-setup.ts',
	timeout: 30_000,
	// Session lifecycle and workspace-membership specs share the one dedicated
	// backend/database pair, so they run one at a time until fixtures prove
	// state is isolated per test.
	fullyParallel: false,
	workers: 1,
	retries: 0,
	reporter: 'list',
	use: {
		baseURL: frontendBaseUrl,
		trace: 'retain-on-failure'
	},
	projects: [
		{
			name: 'chromium',
			use: { ...devices['Desktop Chrome'] },
			testIgnore: /mobile\.spec\.ts/
		},
		// Scoped to its own spec rather than the whole suite: re-running every
		// browser test at a phone viewport would double the run for coverage
		// this suite already gets from the desktop project everywhere except
		// small-viewport layout and touch hard-refresh behavior.
		{ name: 'mobile', use: { ...devices['Pixel 5'] }, testMatch: /mobile\.spec\.ts/ }
	]
});
