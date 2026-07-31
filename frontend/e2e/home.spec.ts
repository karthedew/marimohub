import { expect, test } from '@playwright/test';

import { createBlankNotebook, createWorkspace, register, unique } from './helpers';

test('home page renders the marketing shell and links to discover', async ({ page }) => {
	await page.goto('/');

	await expect(page).toHaveTitle('MarimoHub');
	await expect(page.getByRole('heading', { level: 1 })).toContainText('Discover, fork, and run');
	await expect(page.getByRole('link', { name: 'Browse notebooks' })).toHaveAttribute(
		'href',
		'/discover'
	);
});

test('signed-in home lists Notebooks from the selected Workspace', async ({ page }) => {
	const username = unique('homeuser');
	const title = unique('Home Notebook');
	await register(page, username);
	await createWorkspace(page, 'Home Space');
	const notebookId = await createBlankNotebook(page, title);

	await page.getByRole('link', { name: 'MarimoHub home' }).click();

	await expect(page.getByRole('heading', { name: 'Notebooks' })).toBeVisible();
	await expect(page.getByText('Home Space', { exact: true }).first()).toBeVisible();
	await expect(page.getByRole('link', { name: new RegExp(title) })).toHaveAttribute('href', `/notebooks/${notebookId}`);
	await expect(page.getByRole('link', { name: 'New Notebook', exact: true })).toHaveCount(0);
	await expect(page.getByRole('link', { name: 'New notebook', exact: true })).toBeVisible();
});
