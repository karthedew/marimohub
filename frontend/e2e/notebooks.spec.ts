import http from 'node:http';
import type { AddressInfo } from 'node:net';

import { expect, test } from '@playwright/test';

import { createBlankNotebook, createWorkspace, idFromEditUrl, openWorkspace, register, submitBlankNotebook, unique, userId } from './helpers';

const MARIMO_SOURCE =
	'import marimo as mo\n\napp = mo.App()\n\n\n@app.cell\ndef _():\n    mo.md("# Imported")\n    return\n\n\nif __name__ == "__main__":\n    app.run()\n';

async function serveNotebookSource(source: string) {
	const server = http.createServer((_req, res) => {
		res.writeHead(200, { 'Content-Type': 'text/plain' });
		res.end(source);
	});
	await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
	const { port } = server.address() as AddressInfo;
	return {
		url: `http://127.0.0.1:${port}/notebook.py`,
		close: () => new Promise<void>((resolve) => server.close(() => resolve()))
	};
}

test('a caller with exactly one writable Workspace gets it preselected, and creation sends it', async ({ page }) => {
	await register(page, unique('onetarget'));
	await createWorkspace(page, 'Solo Space');

	await page.goto('/notebooks/new');
	const workspaceSelect = page.getByLabel('Workspace', { exact: true });
	await expect(workspaceSelect).toBeDisabled();
	await expect(workspaceSelect).toHaveValue(/.+/);

	const id = await createBlankNotebook(page, unique('Blank Notebook'));
	await page.goto(`/notebooks/${id}`);
	await expect(page.getByText('Solo Space')).toBeVisible();
});

test('zero writable Workspaces blocks creation and links to Workspace creation', async ({ page }) => {
	await register(page, unique('notargets'));

	await page.goto('/notebooks/new');
	await expect(page.getByText('You need write access to a Workspace first.')).toBeVisible();
	await expect(page.getByRole('link', { name: 'Go to Workspaces' })).toHaveAttribute('href', '/workspaces');
	await expect(page.getByRole('button', { name: 'Create blank notebook' })).toBeDisabled();
});

test('multiple writable Workspaces require a deliberate choice before creating', async ({ page }) => {
	await register(page, unique('multitarget'));
	await createWorkspace(page, 'Alpha Team');
	await createWorkspace(page, 'Beta Team');

	await page.goto('/notebooks/new');
	const workspaceSelect = page.getByLabel('Workspace', { exact: true });
	await expect(workspaceSelect).toHaveValue('');
	await expect(page.getByRole('button', { name: 'Create blank notebook' })).toBeDisabled();

	await workspaceSelect.selectOption({ label: 'Alpha Team' });
	const id = await submitBlankNotebook(page, unique('Alpha Notebook'));
	await page.goto(`/notebooks/${id}`);
	await expect(page.getByText('Alpha Team')).toBeVisible();
});

test('an uploaded file and a GitLab URL import both send the chosen Workspace', async ({ page }) => {
	await register(page, unique('importer'));
	await createWorkspace(page, 'Import Space');

	await page.goto('/notebooks/new');
	await page.getByRole('tab', { name: 'Upload .py' }).click();
	await page.locator('#file').setInputFiles({
		name: 'uploaded.py',
		mimeType: 'text/x-python',
		buffer: Buffer.from(MARIMO_SOURCE)
	});
	await page.getByLabel('Title', { exact: true }).fill(unique('Uploaded Notebook'));
	await page.getByRole('button', { name: 'Create from file' }).click();
	await page.waitForURL(/\/notebooks\/[^/]+\/edit$/);
	const uploadedId = await idFromEditUrl(page);
	await page.goto(`/notebooks/${uploadedId}`);
	await expect(page.getByText('Import Space')).toBeVisible();

	const fixture = await serveNotebookSource(MARIMO_SOURCE);
	try {
		await page.goto('/notebooks/new');
		await page.getByRole('tab', { name: 'GitLab URL' }).click();
		await page.getByLabel('GitLab raw file URL').fill(fixture.url);
		const patField = page.getByLabel('Personal access token', { exact: false });
		await patField.fill('super-secret-token');
		await page.getByRole('button', { name: 'Import from GitLab' }).click();
		await page.waitForURL(/\/notebooks\/[^/]+\/edit$/);
		const importedId = await idFromEditUrl(page);
		await page.goto(`/notebooks/${importedId}`);
		await expect(page.getByText('Import Space')).toBeVisible();

		// The PAT is never retained past the request that used it.
		await page.goto('/notebooks/new');
		await page.getByRole('tab', { name: 'GitLab URL' }).click();
		await expect(page.getByLabel('Personal access token', { exact: false })).toHaveValue('');
	} finally {
		await fixture.close();
	}
});

test('fork always requires an explicit target confirmation, even with a single writable Workspace', async ({ browser }) => {
	test.setTimeout(60_000);
	const ownerContext = await browser.newContext();
	const forkerContext = await browser.newContext();
	const ownerPage = await ownerContext.newPage();
	const forkerPage = await forkerContext.newPage();

	try {
		await register(ownerPage, unique('forksource'));
		await createWorkspace(ownerPage, 'Source Space');
		const notebookId = await createBlankNotebook(ownerPage, unique('Forkable'));
		await ownerPage.goto(`/notebooks/${notebookId}`);
		await ownerPage.getByLabel('Visibility', { exact: true }).selectOption('public');
		// The select's own DOM value flips the instant the browser applies the
		// user's choice, before the publish request round-trips — only the
		// header badge reflects the reloaded, backend-confirmed notebook.
		await expect(ownerPage.getByTestId('visibility-badge')).toHaveText('public');

		await register(forkerPage, unique('forktarget'));
		await createWorkspace(forkerPage, 'Fork Space');

		await forkerPage.goto(`/notebooks/${notebookId}`);
		await forkerPage.getByRole('button', { name: 'Fork' }).click();

		const forkTarget = forkerPage.getByLabel('Fork into', { exact: true });
		await expect(forkTarget).toBeDisabled();
		await expect(forkTarget).toHaveValue(/.+/);

		const confirmButton = forkerPage.getByRole('button', { name: 'Confirm fork' });
		await expect(confirmButton).toBeEnabled();
		await confirmButton.click();
		await forkerPage.waitForURL(/\/notebooks\/[^/]+\/edit$/);

		const forkId = await idFromEditUrl(forkerPage);
		expect(forkId).not.toBe(notebookId);
		await forkerPage.goto(`/notebooks/${forkId}`);
		await expect(forkerPage.getByText('Fork Space')).toBeVisible();
	} finally {
		await ownerContext.close();
		await forkerContext.close();
	}
});

test('a Viewer sees no write controls; promoting to Editor reveals them', async ({ browser }) => {
	const ownerContext = await browser.newContext();
	const memberContext = await browser.newContext();
	const ownerPage = await ownerContext.newPage();
	const memberPage = await memberContext.newPage();

	try {
		await register(ownerPage, unique('capowner'));
		await register(memberPage, unique('capmember'));
		const memberId = await userId(memberPage);

		await createWorkspace(ownerPage, 'Capability Space');
		await openWorkspace(ownerPage, 'Capability Space');
		await ownerPage.getByLabel('User ID').fill(memberId);
		await ownerPage.getByLabel('Role').selectOption('viewer');
		await ownerPage.getByRole('button', { name: 'Add member' }).click();
		await expect(ownerPage.locator('tr', { hasText: memberId })).toBeVisible();

		await ownerPage.goto('/workspaces');
		await openWorkspace(ownerPage, 'Capability Space');
		const notebookId = await createBlankNotebook(ownerPage, unique('Capability Notebook'));

		await memberPage.goto(`/notebooks/${notebookId}`);
		await expect(memberPage.getByRole('link', { name: 'Run' })).toBeVisible();
		await expect(memberPage.getByRole('link', { name: 'Edit' })).toHaveCount(0);
		await expect(memberPage.getByLabel('Visibility', { exact: true })).toHaveCount(0);
		await expect(memberPage.getByRole('button', { name: 'Delete notebook' })).toHaveCount(0);

		await ownerPage.goto('/workspaces');
		await openWorkspace(ownerPage, 'Capability Space');
		await ownerPage.locator('tr', { hasText: memberId }).getByRole('combobox').selectOption('editor');

		await memberPage.reload();
		await expect(memberPage.getByRole('link', { name: 'Edit' })).toBeVisible();
		await expect(memberPage.getByLabel('Visibility', { exact: true })).toBeVisible();
		await expect(memberPage.getByRole('button', { name: 'Delete notebook' })).toBeVisible();
	} finally {
		await ownerContext.close();
		await memberContext.close();
	}
});

test('Visibility moves Private -> Unlisted -> Public -> Private and Discover reflects it', async ({ page }) => {
	test.setTimeout(60_000);
	await register(page, unique('visowner'));
	await createWorkspace(page, 'Visibility Space');
	const title = unique('Visibility Notebook');
	const id = await createBlankNotebook(page, title);
	await page.goto(`/notebooks/${id}`);

	// The select's DOM value flips the instant the browser applies a choice,
	// before the request round-trips — the header badge only updates once the
	// page reloads its data after the backend confirms the change.
	const visibility = page.getByLabel('Visibility', { exact: true });
	const badge = page.getByTestId('visibility-badge');
	await expect(badge).toHaveText('private');

	await visibility.selectOption('unlisted');
	await expect(badge).toHaveText('unlisted');

	await visibility.selectOption('public');
	await expect(badge).toHaveText('public');
	await page.goto(`/discover?q=${encodeURIComponent(title)}`);
	await expect(page.getByRole('link', { name: new RegExp(title) })).toBeVisible();

	await page.goto(`/notebooks/${id}`);
	await page.getByLabel('Visibility', { exact: true }).selectOption('private');
	await expect(page.getByTestId('visibility-badge')).toHaveText('private');
});

test('permanent delete requires confirmation naming the Notebook and returns to Discover', async ({ page }) => {
	await register(page, unique('deleter'));
	await createWorkspace(page, 'Delete Space');
	const title = unique('Doomed Notebook');
	const id = await createBlankNotebook(page, title);
	await page.goto(`/notebooks/${id}`);

	await page.getByRole('button', { name: 'Delete notebook' }).click();
	await expect(page.getByText(`Delete "${title}"?`, { exact: false })).toBeVisible();
	await page.getByRole('button', { name: 'Yes, delete this notebook' }).click();
	await page.waitForURL('**/discover');

	await page.goto(`/notebooks/${id}`);
	await expect(page.getByRole('heading', { name: 'Notebook unavailable' })).toBeVisible();
});

test('a Private Notebook survives an authenticated hard refresh', async ({ page }) => {
	await register(page, unique('refresher'));
	await createWorkspace(page, 'Refresh Space');
	const title = unique('Refresh Notebook');
	const id = await createBlankNotebook(page, title);

	await page.goto(`/notebooks/${id}`);
	await expect(page.getByRole('heading', { name: title })).toBeVisible();

	await page.reload();
	await expect(page.getByRole('heading', { name: title })).toBeVisible();
	await expect(page.getByRole('link', { name: 'Edit' })).toBeVisible();
});

test('multi-tag filtering sends repeated parameters and returns only Notebooks with every tag', async ({ page }) => {
	await register(page, unique('tagger'));
	await createWorkspace(page, 'Tag Space');

	await page.goto('/notebooks/new');
	await page.getByLabel('Title', { exact: true }).fill('Overlap Notebook');
	await page.getByLabel('Tags', { exact: true }).fill('ml, viz');
	await page.getByRole('button', { name: 'Create blank notebook' }).click();
	await page.waitForURL(/\/notebooks\/[^/]+\/edit$/);

	await page.goto('/notebooks/new');
	await page.getByLabel('Title', { exact: true }).fill('ML Only Notebook');
	await page.getByLabel('Tags', { exact: true }).fill('ml');
	await page.getByRole('button', { name: 'Create blank notebook' }).click();
	await page.waitForURL(/\/notebooks\/[^/]+\/edit$/);

	await page.goto('/discover?tags=ml%2Cviz');
	await expect(page.getByRole('link', { name: /Overlap Notebook/ })).toBeVisible();
	await expect(page.getByRole('link', { name: /ML Only Notebook/ })).toHaveCount(0);
});
