import type { Page } from '@playwright/test';

export function unique(label: string) {
	return `${label}${Date.now()}${Math.floor(Math.random() * 10_000)}`;
}

export async function register(page: Page, username: string) {
	await page.goto('/auth/register');
	await page.getByLabel('Username').fill(username);
	await page.getByLabel('Email').fill(`${username}@example.com`);
	await page.getByLabel('Password').fill('password123');
	await page.getByRole('button', { name: 'Create account' }).click();
	await page.waitForURL((url) => url.pathname === '/');
}

export async function userId(page: Page) {
	return page.evaluate(() => {
		const raw = localStorage.getItem('marimohub-auth');
		return raw ? (JSON.parse(raw).currentUser?.id as string | undefined) ?? '' : '';
	});
}

export async function createWorkspace(page: Page, name: string, slug?: string) {
	await page.goto('/workspaces');
	// exact: true avoids matching the Slug field, whose helper copy ("...
	// generated from the name...") otherwise makes it a substring hit for "Name".
	await page.getByLabel('Name', { exact: true }).fill(name);
	if (slug) await page.getByLabel('Slug (optional)').fill(slug);
	await page.getByRole('button', { name: 'Create workspace' }).click();
}

export async function openWorkspace(page: Page, name: string) {
	await page.getByRole('link', { name: new RegExp(name) }).click();
	await page.waitForURL(/\/workspaces\/[^/]+$/);
}

// Notebook create always redirects to the new notebook's editor. Callers that
// only need to know which Workspace the created Notebook landed in navigate
// straight to its detail page instead of waiting on a real marimo session.
export async function idFromEditUrl(page: Page) {
	const match = page.url().match(/\/notebooks\/([^/]+)\/edit$/);
	if (!match) throw new Error(`expected an edit URL, got ${page.url()}`);
	return match[1];
}

export async function submitBlankNotebook(page: Page, title: string) {
	await page.getByLabel('Title', { exact: true }).fill(title);
	await page.getByRole('button', { name: 'Create blank notebook' }).click();
	await page.waitForURL(/\/notebooks\/[^/]+\/edit$/);
	return idFromEditUrl(page);
}

export async function createBlankNotebook(page: Page, title: string) {
	await page.goto('/notebooks/new');
	return submitBlankNotebook(page, title);
}
