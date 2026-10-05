import type { APIRequestContext, Page } from '@playwright/test';

import { backendBaseUrl } from './env';

export function unique(label: string) {
	return `${label}${Date.now()}${Math.floor(Math.random() * 10_000)}`;
}

// Registers someone straight through the API, for a test that needs them to
// exist but never signs in as them: much faster than the registration form.
export async function registerByApi(
	request: APIRequestContext,
	username: string,
	options: { displayName?: string } = {}
) {
	const response = await request.post(`${backendBaseUrl}/api/auth/register`, {
		data: {
			username,
			email: `${username}@example.com`,
			password: 'password123',
			...(options.displayName ? { display_name: options.displayName } : {})
		}
	});
	if (!response.ok()) {
		throw new Error(`registering ${username}: ${response.status()} ${await response.text()}`);
	}
	return (await response.json()) as { id: string };
}

export async function register(page: Page, username: string, options: { displayName?: string } = {}) {
	await page.goto('/auth/register');
	if (options.displayName) await page.getByLabel('Full name').fill(options.displayName);
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

// Adds a member from the open workspace page the way an Owner does: searches
// the Person combobox, chooses the first person offered (an exact username or
// a pasted User ID always ranks first), then submits with `role`.
export async function addMember(page: Page, search: string, role: 'owner' | 'editor' | 'viewer') {
	await page.getByRole('combobox', { name: 'Person' }).fill(search);
	await page.getByRole('listbox', { name: 'Matching people' }).getByRole('option').first().click();
	await page.getByLabel('Role').selectOption(role);
	await page.getByRole('button', { name: 'Add member' }).click();
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
