import { expect, test } from '@playwright/test';

import { createWorkspace, openWorkspace, register, unique, userId } from './helpers';

test('registering with no return path lands on the Notebook home', async ({ page }) => {
	await register(page, unique('reguser'));

	await expect(page).toHaveURL(/\/$/);
	await expect(page.getByRole('heading', { name: 'Notebooks' })).toBeVisible();
	await expect(page.getByRole('heading', { name: 'Create your first Workspace' })).toBeVisible();
});

test('creates a workspace and surfaces a slug conflict inline instead of navigating away', async ({ page }) => {
	await register(page, unique('creator'));

	const slug = unique('team').toLowerCase();
	await createWorkspace(page, 'Team One', slug);
	await expect(page.getByRole('link', { name: /Team One/ })).toBeVisible();

	await createWorkspace(page, 'Team Two', slug);
	await expect(page).toHaveURL(/\/workspaces$/);
	await expect(page.getByRole('alert')).toContainText(/already in use/i);
	await expect(page.getByRole('link', { name: /Team Two/ })).toHaveCount(0);
});

test('copies the signed-in User ID to the clipboard', async ({ page, context, baseURL }) => {
	await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: baseURL });
	await register(page, unique('copyuser'));

	const id = await userId(page);
	expect(id.length).toBeGreaterThan(0);

	await page.goto('/settings');
	await page.getByRole('button', { name: 'Copy' }).click();
	await expect(page.getByRole('button', { name: 'Copied' })).toBeVisible();

	const clipboardText = await page.evaluate(() => navigator.clipboard.readText());
	expect(clipboardText).toBe(id);
});

test('an owner can add, promote, demote, and remove a member; the removed member loses access', async ({ browser }) => {
	const ownerContext = await browser.newContext();
	const memberContext = await browser.newContext();
	const ownerPage = await ownerContext.newPage();
	const memberPage = await memberContext.newPage();

	try {
		await register(ownerPage, unique('owner'));
		await register(memberPage, unique('member'));
		const memberId = await userId(memberPage);

		await createWorkspace(ownerPage, 'Collab Space');
		await openWorkspace(ownerPage, 'Collab Space');

		await ownerPage.getByLabel('User ID').fill(memberId);
		await ownerPage.getByLabel('Role').selectOption('viewer');
		await ownerPage.getByRole('button', { name: 'Add member' }).click();
		const memberRow = ownerPage.locator('tr', { hasText: memberId });
		await expect(memberRow).toBeVisible();
		await expect(memberRow.getByRole('combobox')).toHaveValue('viewer');

		await memberPage.goto('/workspaces');
		await expect(memberPage.getByRole('link', { name: /Collab Space/ })).toBeVisible();

		await memberRow.getByRole('combobox').selectOption('editor');
		await expect(memberRow.getByRole('combobox')).toHaveValue('editor');

		await memberRow.getByRole('combobox').selectOption('viewer');
		await expect(memberRow.getByRole('combobox')).toHaveValue('viewer');

		await memberRow.getByRole('button', { name: 'Remove' }).click();
		await expect(ownerPage.locator('tr', { hasText: memberId })).toHaveCount(0);

		await memberPage.reload();
		await expect(memberPage.getByRole('link', { name: /Collab Space/ })).toHaveCount(0);
	} finally {
		await ownerContext.close();
		await memberContext.close();
	}
});

test('the backend refuses to demote or remove the sole owner of a workspace', async ({ page }) => {
	await register(page, unique('soleowner'));
	const id = await userId(page);

	await createWorkspace(page, 'Solo Space');
	await openWorkspace(page, 'Solo Space');

	const ownRow = page.locator('tr', { hasText: id });
	await ownRow.getByRole('combobox').selectOption('editor');
	await expect(page.getByRole('alert')).toContainText(/at least one owner/i);

	await ownRow.getByRole('button', { name: 'Remove' }).click();
	await expect(page.getByRole('alert')).toContainText(/at least one owner/i);
});

test('archiving a workspace returns to Workspaces, and restore brings it back', async ({ page }) => {
	await register(page, unique('archiver'));

	await createWorkspace(page, 'Scratch Space');
	await openWorkspace(page, 'Scratch Space');

	await page.getByRole('button', { name: 'Archive workspace' }).click();
	await page.getByRole('button', { name: 'Yes, archive this workspace' }).click();

	await page.waitForURL(/\/workspaces\?notice=workspace-archived$/);
	await expect(page.getByRole('status')).toContainText(/archived/i);
	await expect(page.getByRole('link', { name: /Scratch Space/ })).toHaveCount(0);

	await page.getByRole('link', { name: 'Archived workspaces' }).click();
	await expect(page.getByText('Scratch Space')).toBeVisible();

	await page.getByRole('button', { name: 'Restore' }).click();
	await expect(page.getByText('Scratch Space')).toHaveCount(0);

	await page.goto('/workspaces');
	await expect(page.getByRole('link', { name: /Scratch Space/ })).toBeVisible();
});

test('only the Owner sees Rename; an Editor sees the name as read-only', async ({ browser }) => {
	const ownerContext = await browser.newContext();
	const editorContext = await browser.newContext();
	const ownerPage = await ownerContext.newPage();
	const editorPage = await editorContext.newPage();

	try {
		await register(ownerPage, unique('renameowner'));
		await register(editorPage, unique('renameeditor'));
		const editorId = await userId(editorPage);

		await createWorkspace(ownerPage, 'Rename Space');
		await openWorkspace(ownerPage, 'Rename Space');
		await ownerPage.getByLabel('User ID').fill(editorId);
		await ownerPage.getByLabel('Role').selectOption('editor');
		await ownerPage.getByRole('button', { name: 'Add member' }).click();
		await expect(ownerPage.locator('tr', { hasText: editorId })).toBeVisible();

		await expect(ownerPage.getByRole('button', { name: 'Rename' })).toBeVisible();
		await ownerPage.getByRole('button', { name: 'Rename' }).click();
		await ownerPage.getByLabel('Name', { exact: true }).fill('Renamed Space');
		await ownerPage.getByRole('button', { name: 'Save' }).click();
		await expect(ownerPage.getByRole('heading', { name: 'Renamed Space' })).toBeVisible();

		await editorPage.goto('/workspaces');
		await openWorkspace(editorPage, 'Renamed Space');
		await expect(editorPage.getByRole('heading', { name: 'Renamed Space' })).toBeVisible();
		await expect(editorPage.getByRole('button', { name: 'Rename' })).toHaveCount(0);
		// An Editor administers Notebooks, not the Workspace itself: member
		// management and archive stay Owner-only regardless of write capability.
		await expect(editorPage.getByLabel('User ID')).toHaveCount(0);
		await expect(editorPage.getByRole('button', { name: 'Archive workspace' })).toHaveCount(0);
	} finally {
		await ownerContext.close();
		await editorContext.close();
	}
});

test('every workspace route survives a hard refresh while authenticated', async ({ page }) => {
	await register(page, unique('refresher'));

	await createWorkspace(page, 'Refresh Space');
	await openWorkspace(page, 'Refresh Space');
	const detailUrl = page.url();

	await page.reload();
	await expect(page.getByRole('heading', { name: 'Refresh Space' })).toBeVisible();

	await page.goto('/workspaces');
	await page.reload();
	await expect(page.getByRole('heading', { name: 'Workspaces' })).toBeVisible();

	await page.goto('/workspaces/archived');
	await page.reload();
	await expect(page.getByRole('heading', { name: 'Archived workspaces' })).toBeVisible();

	await page.goto(detailUrl);
	await page.reload();
	await expect(page.getByRole('heading', { name: 'Refresh Space' })).toBeVisible();
});
