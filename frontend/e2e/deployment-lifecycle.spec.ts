import { expect, test } from '@playwright/test';

import { backendBaseUrl } from './env';
import { addMember, createBlankNotebook, createWorkspace, openWorkspace, register, unique, userId } from './helpers';

async function authToken(page: import('@playwright/test').Page) {
	return page.evaluate(() => {
		const raw = localStorage.getItem('marimohub-auth');
		return raw ? (JSON.parse(raw).token as string | undefined) : undefined;
	});
}

async function deployFromDetail(page: import('@playwright/test').Page, notebookId: string) {
	await page.goto(`/notebooks/${notebookId}`);
	await page.getByRole('button', { name: 'Deploy' }).click();
	const status = page.getByTestId('deployment-status');
	await expect(status).toHaveText(/running|sleeping/);
	const slug = (await page.getByRole('link', { name: /deploy\// }).getAttribute('href'))?.split('/deploy/')[1];
	if (!slug) throw new Error('expected a public deployment link after deploying');
	return slug;
}

test('a reader sees the active Deployment link; only an Editor/Owner sees Stop', async ({ browser }) => {
	test.setTimeout(60_000);
	const ownerContext = await browser.newContext();
	const viewerContext = await browser.newContext();
	const ownerPage = await ownerContext.newPage();
	const viewerPage = await viewerContext.newPage();

	try {
		const viewerName = unique('deployviewer');
		await register(ownerPage, unique('deployowner'));
		await register(viewerPage, viewerName);
		const viewerId = await userId(viewerPage);

		await createWorkspace(ownerPage, 'Deploy Space');
		await openWorkspace(ownerPage, 'Deploy Space');
		await addMember(ownerPage, viewerName, 'viewer');
		await expect(ownerPage.locator('tr', { hasText: viewerId })).toBeVisible();

		await ownerPage.goto('/workspaces');
		await openWorkspace(ownerPage, 'Deploy Space');
		const notebookId = await createBlankNotebook(ownerPage, unique('Deploy Notebook'));

		await deployFromDetail(ownerPage, notebookId);
		await expect(ownerPage.getByRole('button', { name: 'Stop deployment' })).toBeVisible();

		await viewerPage.goto(`/notebooks/${notebookId}`);
		await expect(viewerPage.getByTestId('deployment-status')).toHaveText(/running|sleeping/);
		await expect(viewerPage.getByRole('link', { name: 'Open public deployment' })).toBeVisible();
		await expect(viewerPage.getByRole('button', { name: 'Stop deployment' })).toHaveCount(0);
	} finally {
		await ownerContext.close();
		await viewerContext.close();
	}
});

test('a Private Notebook Deployment still shows the public-access warning', async ({ page }) => {
	test.setTimeout(60_000);
	await register(page, unique('privatedeploy'));
	await createWorkspace(page, 'Private Deploy Space');
	const notebookId = await createBlankNotebook(page, unique('Private Deploy Notebook'));

	await page.goto(`/notebooks/${notebookId}`);
	await expect(page.getByTestId('visibility-badge')).toHaveText('private');

	await deployFromDetail(page, notebookId);
	await expect(page.getByText(/public to anyone with the link/i)).toBeVisible();
});

test('stopping a Deployment out-of-band is reflected after a focus refresh, without a reload', async ({ page }) => {
	test.setTimeout(60_000);
	await register(page, unique('focuspoll'));
	await createWorkspace(page, 'Focus Space');
	const notebookId = await createBlankNotebook(page, unique('Focus Notebook'));

	const slug = await deployFromDetail(page, notebookId);
	const token = await authToken(page);

	const stopped = await page.request.delete(`${backendBaseUrl}/api/deployments/${slug}`, {
		headers: { Authorization: `Bearer ${token}` }
	});
	expect(stopped.ok()).toBe(true);

	// No reload — only a focus event, the same signal the real browser fires
	// when the tab regains focus, should be enough to pick up the change.
	await page.evaluate(() => window.dispatchEvent(new Event('focus')));

	await expect(page.getByTestId('deployment-status')).toHaveCount(0);
	await expect(page.getByRole('button', { name: 'Deploy again' })).toBeVisible();
});

test('stopping and redeploying from the notebook detail controls refreshes status in place', async ({ page }) => {
	test.setTimeout(60_000);
	await register(page, unique('stopredeploy'));
	await createWorkspace(page, 'Redeploy Space');
	const notebookId = await createBlankNotebook(page, unique('Redeploy Notebook'));

	const firstSlug = await deployFromDetail(page, notebookId);
	await page.getByRole('button', { name: 'Stop deployment' }).click();
	await expect(page.getByTestId('deployment-status')).toHaveCount(0);
	await expect(page.getByRole('button', { name: 'Deploy again' })).toBeVisible();

	await page.getByRole('button', { name: 'Deploy again' }).click();
	const status = page.getByTestId('deployment-status');
	await expect(status).toHaveText(/running|sleeping/);
	const secondSlug = (await page.getByRole('link', { name: /deploy\// }).getAttribute('href'))?.split('/deploy/')[1];
	expect(secondSlug).toBe(firstSlug);
	await expect(page.getByRole('button', { name: 'Stop deployment' })).toBeVisible();
});

test('a sleeping Deployment wakes through the bounded retry and opens', async ({ page }) => {
	test.setTimeout(60_000);
	await register(page, unique('wakeup'));
	await createWorkspace(page, 'Wake Space');
	const notebookId = await createBlankNotebook(page, unique('Wake Notebook'));
	const slug = await deployFromDetail(page, notebookId);

	await page.goto(`/deploy/${slug}`);
	await expect(page.getByRole('heading', { name: `Starting ${slug}` })).toBeVisible();
	await expect(page.locator('iframe')).toBeVisible({ timeout: 20_000 });
});

test('429 capacity exhaustion stops retrying and requires a manual Retry', async ({ page }) => {
	test.setTimeout(60_000);
	await register(page, unique('capacity'));
	await createWorkspace(page, 'Capacity Space');
	const notebookId = await createBlankNotebook(page, unique('Capacity Notebook'));
	const slug = await deployFromDetail(page, notebookId);

	let intercepted = 0;
	await page.route(`**/api/deployments/${slug}`, async (route) => {
		// The GET carries an Authorization header, so the browser sends a CORS
		// preflight OPTIONS first — that one must reach the real backend so it
		// gets a proper CORS response, or the browser blocks the GET as a CORS
		// failure before this mock ever gets to matter.
		if (route.request().method() !== 'GET') {
			await route.continue();
			return;
		}
		intercepted += 1;
		await route.fulfill({ status: 429, contentType: 'application/json', body: JSON.stringify({ detail: 'No capacity available' }) });
	});

	await page.goto(`/deploy/${slug}`);
	await expect(page.getByRole('heading', { name: 'Unable to open this app.' })).toBeVisible();
	await expect(page.getByText(/at capacity/i)).toBeVisible();
	const retryButton = page.getByRole('button', { name: 'Try again' });
	await expect(retryButton).toBeVisible();

	const countAfterFailure = intercepted;
	await page.waitForTimeout(2000);
	// A 429 must never be retried on its own timer — only the count from the
	// user's own click below is allowed to move it further.
	expect(intercepted).toBe(countAfterFailure);

	await page.unroute(`**/api/deployments/${slug}`);
	await retryButton.click();
	await expect(page.locator('iframe')).toBeVisible({ timeout: 20_000 });
});

test('a Viewer who opens the edit route directly is told an Editor role is required', async ({ browser }) => {
	test.setTimeout(60_000);
	const ownerContext = await browser.newContext();
	const viewerContext = await browser.newContext();
	const ownerPage = await ownerContext.newPage();
	const viewerPage = await viewerContext.newPage();

	try {
		const viewerName = unique('editauthviewer');
		await register(ownerPage, unique('editauthowner'));
		await register(viewerPage, viewerName);
		const viewerId = await userId(viewerPage);

		await createWorkspace(ownerPage, 'Edit Auth Space');
		await openWorkspace(ownerPage, 'Edit Auth Space');
		await addMember(ownerPage, viewerName, 'viewer');
		await expect(ownerPage.locator('tr', { hasText: viewerId })).toBeVisible();

		await ownerPage.goto('/workspaces');
		await openWorkspace(ownerPage, 'Edit Auth Space');
		const notebookId = await createBlankNotebook(ownerPage, unique('Edit Auth Notebook'));

		await viewerPage.goto(`/notebooks/${notebookId}/edit`);
		await expect(viewerPage.getByText('Editor role required')).toBeVisible();
		await expect(viewerPage.getByText('Editor role is required to edit this notebook.')).toBeVisible();
		await viewerPage.getByRole('link', { name: 'Back to notebook' }).click();
		await expect(viewerPage).toHaveURL(new RegExp(`/notebooks/${notebookId}$`));
	} finally {
		await ownerContext.close();
		await viewerContext.close();
	}
});

test('in-app navigation away from an edit session waits for the session to end first', async ({ page }) => {
	test.setTimeout(60_000);
	await register(page, unique('navteardown'));
	await createWorkspace(page, 'Nav Space');
	const notebookId = await createBlankNotebook(page, unique('Nav Notebook'));

	// createBlankNotebook already landed on the edit route; wait for the real
	// session to finish starting so there is something for navigation to tear
	// down.
	const created = await page.waitForResponse((response) => response.url().endsWith('/api/sessions') && response.request().method() === 'POST');
	const session = (await created.json()) as { id: string };

	let deleteResolved = false;
	let deleteCount = 0;
	page.on('response', (response) => {
		if (response.url() === `${backendBaseUrl}/api/sessions/${session.id}` && response.request().method() === 'DELETE') {
			deleteResolved = true;
			deleteCount += 1;
		}
	});

	// SvelteKit preloads the destination's data on hover, ahead of the click —
	// that fetch is independent of the actual navigation, so it cannot be used
	// to prove ordering. The URL itself can: `beforeNavigate` cancels the
	// transition synchronously, so the browser's location cannot flip to the
	// destination until the programmatic `goto` after the awaited DELETE runs.
	await page.getByRole('link', { name: 'MarimoHub home' }).click();
	await page.waitForURL((url) => url.pathname === '/');

	expect(deleteResolved).toBe(true);
	expect(deleteCount).toBe(1);
	await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
});

test('editing a cell persists through marimo autosave and survives in-app navigation, without the removed session-save endpoint', async ({ page }) => {
	test.setTimeout(60_000);
	await register(page, unique('autosave'));
	await createWorkspace(page, 'Autosave Space');
	const notebookTitle = unique('Autosave Notebook');
	const notebookId = await createBlankNotebook(page, notebookTitle);

	const marker = unique('autosave-marker');
	const requestUrls: string[] = [];
	page.on('request', (request) => requestUrls.push(request.url()));

	const cellContent = page.frameLocator('iframe').locator('.cm-content').first();
	await expect(cellContent).toBeVisible({ timeout: 20_000 });
	await cellContent.click();
	await page.keyboard.press('ControlOrMeta+A');
	await page.keyboard.type(`# ${marker}`);

	// Autosave is entirely marimo's own behavior, relayed through the proxy —
	// this frontend never calls a save endpoint of its own. The proxied
	// `POST .../api/kernel/save` marimo fires after its configured autosave
	// delay is the one reliable signal the edit actually persisted; waiting on
	// it beats a fixed timeout because it reflects the real write instead of
	// guessing how long autosave takes.
	const saved = await page.waitForResponse(
		(response) => response.url().includes('/api/kernel/save') && response.request().method() === 'POST'
	);
	expect(saved.ok()).toBe(true);

	// Both hops are in-app client-side transitions, not reloads: away via the
	// global MarimoHub link, then back through the Notebook list and detail.
	await page.getByRole('link', { name: 'MarimoHub home' }).click();
	await page.waitForURL((url) => url.pathname === '/');
	await page.getByRole('link', { name: new RegExp(notebookTitle) }).click();
	await page.waitForURL(new RegExp(`/notebooks/${notebookId}$`));
	await page.getByRole('link', { name: 'Edit' }).click();
	await page.waitForURL(new RegExp(`/notebooks/${notebookId}/edit$`));

	const reopenedCellContent = page.frameLocator('iframe').locator('.cm-content').first();
	await expect(reopenedCellContent).toContainText(marker, { timeout: 20_000 });

	// The removed explicit session-save endpoint was `POST /api/sessions/{id}/save`.
	// A blind substring match on "/save" would also flag marimo's own
	// `/api/kernel/save` autosave call awaited above and the editor's
	// "save-*.js" asset chunk, so the check has to target the deleted
	// endpoint's exact shape rather than the bare word.
	const removedSaveEndpoint = /\/api\/sessions\/[^/?]+\/save(?:[/?]|$)/;
	expect(requestUrls.filter((url) => removedSaveEndpoint.test(url))).toEqual([]);
});

test('a hard refresh of an edit session does not produce more than one DELETE for the original session', async ({ page }) => {
	test.setTimeout(60_000);
	await register(page, unique('refreshteardown'));
	await createWorkspace(page, 'Refresh Teardown Space');
	await createBlankNotebook(page, unique('Refresh Teardown Notebook'));

	const created = await page.waitForResponse((response) => response.url().endsWith('/api/sessions') && response.request().method() === 'POST');
	const session = (await created.json()) as { id: string };

	let deletesForOriginalSession = 0;
	page.on('response', (response) => {
		if (response.url() === `${backendBaseUrl}/api/sessions/${session.id}` && response.request().method() === 'DELETE') {
			deletesForOriginalSession += 1;
		}
	});

	await page.reload();
	// The reload's own new session must finish starting before the assertion
	// window closes, so there is a definite point past which the original
	// session's unload teardown (fire-and-forget, no ordering guarantee) has
	// had every reasonable chance to arrive.
	await page.waitForResponse((response) => response.url().endsWith('/api/sessions') && response.request().method() === 'POST');
	await page.waitForTimeout(1000);

	expect(deletesForOriginalSession).toBeLessThanOrEqual(1);
});
