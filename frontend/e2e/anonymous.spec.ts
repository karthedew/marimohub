import { expect, test } from '@playwright/test';

import { createBlankNotebook, createWorkspace, register, unique } from './helpers';

async function publicNotebook(browser: import('@playwright/test').Browser, label: string) {
	const context = await browser.newContext();
	const page = await context.newPage();
	await register(page, unique(`${label}owner`));
	await createWorkspace(page, `${label} Space`);
	const id = await createBlankNotebook(page, unique(`${label} Notebook`));
	await page.goto(`/notebooks/${id}`);
	await page.getByLabel('Visibility', { exact: true }).selectOption('public');
	await expect(page.getByTestId('visibility-badge')).toHaveText('public');
	await context.close();
	return id;
}

test('an anonymous visitor can read and run a Public notebook but sees no write controls', async ({ browser }) => {
	test.setTimeout(60_000);
	const notebookId = await publicNotebook(browser, 'anonread');

	const anonContext = await browser.newContext();
	const anonPage = await anonContext.newPage();
	try {
		await anonPage.goto(`/notebooks/${notebookId}`);
		await expect(anonPage.getByRole('heading', { level: 1 })).toBeVisible();
		await expect(anonPage.getByTestId('visibility-badge')).toHaveText('public');

		await expect(anonPage.getByRole('link', { name: 'Run' })).toBeVisible();
		await expect(anonPage.getByRole('link', { name: 'Edit' })).toHaveCount(0);
		await expect(anonPage.getByLabel('Visibility', { exact: true })).toHaveCount(0);
		await expect(anonPage.getByRole('button', { name: 'Delete notebook' })).toHaveCount(0);
		await expect(anonPage.getByRole('link', { name: 'Sign in to fork' })).toHaveAttribute(
			'href',
			`/auth/login?next=${encodeURIComponent(`/notebooks/${notebookId}`)}`
		);

		await anonPage.getByRole('link', { name: 'Run' }).click();
		await anonPage.waitForURL(new RegExp(`/notebooks/${notebookId}/run$`));
		await expect(anonPage.locator('iframe')).toBeVisible({ timeout: 20_000 });
	} finally {
		await anonContext.close();
	}
});

test('signing in from a fork prompt returns the visitor to the notebook that required it, not the default Workspaces landing', async ({ browser }) => {
	test.setTimeout(60_000);
	const notebookId = await publicNotebook(browser, 'loginreturn');

	const anonContext = await browser.newContext();
	const anonPage = await anonContext.newPage();
	try {
		await anonPage.goto(`/notebooks/${notebookId}`);
		await anonPage.getByRole('link', { name: 'Sign in to fork' }).click();
		await anonPage.waitForURL(new RegExp(`/auth/login\\?next=${encodeURIComponent(`/notebooks/${notebookId}`)}`));

		const registerLink = anonPage.getByRole('link', { name: 'Create an account' });
		await expect(registerLink).toHaveAttribute(
			'href',
			`/auth/register?next=${encodeURIComponent(`/notebooks/${notebookId}`)}`
		);
		await registerLink.click();
		await anonPage.waitForURL(new RegExp(`/auth/register\\?next=${encodeURIComponent(`/notebooks/${notebookId}`)}`));
		await expect(anonPage.getByRole('heading', { name: /create your marimohub account/i })).toBeVisible();

		const username = unique('loginreturnvisitor');
		await anonPage.getByLabel('Username').fill(username);
		await anonPage.getByLabel('Email').fill(`${username}@example.com`);
		await anonPage.getByLabel('Password').fill('password123');
		await anonPage.getByRole('button', { name: 'Create account' }).click();

		await anonPage.waitForURL(new RegExp(`/notebooks/${notebookId}$`));
		await expect(anonPage.getByRole('button', { name: 'Fork' })).toBeVisible();
		await expect(anonPage.getByRole('link', { name: 'Sign in to fork' })).toHaveCount(0);
	} finally {
		await anonContext.close();
	}
});
