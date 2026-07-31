import { expect, test } from '@playwright/test';

import { createBlankNotebook, createWorkspace, openWorkspace, register, unique } from './helpers';

// This project runs at a phone viewport (see playwright.config.ts). Desktop
// already exercises every flow in depth; this file only re-checks that the
// same routes survive a hard refresh at a small, touch-sized viewport rather
// than re-running the full suite twice.

test('anonymous marketing and auth routes survive a hard refresh on a phone viewport', async ({ page }) => {
	await page.goto('/');
	await page.reload();
	await expect(page.getByRole('heading', { level: 1 })).toBeVisible();

	await page.goto('/discover');
	await page.reload();
	await expect(page.getByRole('heading', { name: 'Browse notebooks' })).toBeVisible();

	await page.goto('/auth/login');
	await page.reload();
	await expect(page.getByRole('heading', { name: /sign in/i })).toBeVisible();

	await page.goto('/auth/register');
	await page.reload();
	await expect(page.getByRole('heading', { name: /create your marimohub account/i })).toBeVisible();
});

test('the authenticated Workspace and Notebook routes survive a hard refresh on a phone viewport', async ({ page }) => {
	test.setTimeout(60_000);
	await register(page, unique('mobileflow'));

	await page.reload();
	await expect(page.getByRole('heading', { name: 'Notebooks' })).toBeVisible();

	await createWorkspace(page, 'Mobile Space');
	await openWorkspace(page, 'Mobile Space');
	await page.reload();
	await expect(page.getByRole('heading', { name: 'Mobile Space' })).toBeVisible();

	await page.goto('/workspaces/archived');
	await page.reload();
	await expect(page.getByRole('heading', { name: 'Archived workspaces' })).toBeVisible();

	const title = unique('Mobile Notebook');
	const notebookId = await createBlankNotebook(page, title);
	await page.goto(`/notebooks/${notebookId}`);
	await page.reload();
	await expect(page.getByRole('heading', { name: title })).toBeVisible();

	await page.goto(`/notebooks/${notebookId}/edit`);
	const cellContent = page.frameLocator('iframe').locator('.cm-content').first();
	await expect(cellContent).toBeVisible({ timeout: 20_000 });
	await page.reload();
	await expect(page.frameLocator('iframe').locator('.cm-content').first()).toBeVisible({ timeout: 20_000 });

	await page.goto(`/notebooks/${notebookId}/run`);
	await expect(page.locator('iframe')).toBeVisible({ timeout: 20_000 });
	await page.reload();
	await expect(page.locator('iframe')).toBeVisible({ timeout: 20_000 });
});

test('a Deployment route survives a hard refresh on a phone viewport', async ({ page }) => {
	test.setTimeout(60_000);
	await register(page, unique('mobiledeploy'));
	await createWorkspace(page, 'Mobile Deploy Space');
	const notebookId = await createBlankNotebook(page, unique('Mobile Deploy Notebook'));

	await page.goto(`/notebooks/${notebookId}`);
	await page.getByRole('button', { name: 'Deploy' }).click();
	await expect(page.getByTestId('deployment-status')).toHaveText(/running|sleeping/);
	const slug = (await page.getByRole('link', { name: /deploy\// }).getAttribute('href'))?.split('/deploy/')[1];
	if (!slug) throw new Error('expected a public deployment link after deploying');

	await page.goto(`/deploy/${slug}`);
	await page.reload();
	await expect(page.locator('iframe')).toBeVisible({ timeout: 20_000 });
});
