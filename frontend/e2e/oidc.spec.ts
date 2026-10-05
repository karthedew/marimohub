import { expect, test, type Page } from '@playwright/test';

import { backendBaseUrl, e2eOidcProvider, idpControlUrl } from './env';
import { resetFakeIdp, scriptFakeIdpLogin } from './fakeIdp';
import { unique } from './helpers';

// Sign-in through an external identity provider, end to end: the browser
// leaves for the backend's login route, the fake provider from `fakeIdp.ts`
// approves (or denies) at once, and the backend hands back to `/auth/callback`.

const providerButton = `Continue with ${e2eOidcProvider.display_name}`;
const backendCallbackUrl = `${backendBaseUrl}/api/auth/oidc/${e2eOidcProvider.slug}/callback`;
const exchangeUrl = `${backendBaseUrl}/api/auth/oidc/exchange`;

test.beforeEach(async () => {
	await resetFakeIdp(idpControlUrl);
});

// Queues a fresh identity for the next sign-in at the fake provider and returns
// its preferred_username, which the backend uses as the new account's username
// (it never derives one from the email).
async function nextProviderUser(label: string) {
	const username = unique(label);
	await scriptFakeIdpLogin(idpControlUrl, {
		sub: `e2e|${username}`,
		email: `${username}@example.com`,
		preferred_username: username,
		name: 'Provider User'
	});
	return username;
}

function storedSession(page: Page) {
	return page.evaluate(() => localStorage.getItem('marimohub-auth'));
}

function storedAttempt(page: Page) {
	return page.evaluate(() => sessionStorage.getItem('marimohub-oidc'));
}

test('signing in through the Test IdP from the login page lands signed in on the requested page', async ({ page }) => {
	const username = await nextProviderUser('oidcuser');

	await page.goto(`/auth/login?next=${encodeURIComponent('/settings')}`);
	await page.getByRole('button', { name: providerButton }).click();

	await page.waitForURL((url) => url.pathname === '/settings');
	await expect(page.getByRole('heading', { name: 'Profile', exact: true })).toBeVisible();
	await expect(page.getByText(username, { exact: true })).toBeVisible();
	expect(page.url()).not.toContain('handoff');
	expect(await storedAttempt(page)).toBeNull();
});

test('cancelling at the identity provider returns to sign-in with an explanation, still signed out', async ({ page }) => {
	await scriptFakeIdpLogin(idpControlUrl, { deny: true });

	await page.goto(`/auth/login?next=${encodeURIComponent('/workspaces')}`);
	await page.getByRole('button', { name: providerButton }).click();

	await page.waitForURL((url) => url.pathname === '/auth/login' && url.searchParams.get('error') === 'oidc_denied');
	await expect(page.getByRole('alert')).toContainText('Sign-in was cancelled');
	// The backend's error redirect cannot carry `next`; the page recovers it
	// from the abandoned attempt so signing in another way still lands there.
	await expect(page.getByRole('link', { name: 'Create an account' })).toHaveAttribute(
		'href',
		`/auth/register?next=${encodeURIComponent('/workspaces')}`
	);
	expect(await storedSession(page)).toBeNull();
	expect(await storedAttempt(page)).toBeNull();
});

test('replaying a handoff URL from history does not sign anyone in', async ({ page, browser }) => {
	await nextProviderUser('oidcreplay');

	const handoffRedirect = page.waitForResponse((response) => response.url().startsWith(backendCallbackUrl));
	await page.goto(`/auth/login?next=${encodeURIComponent('/settings')}`);
	await page.getByRole('button', { name: providerButton }).click();

	const redirect = await handoffRedirect;
	expect(redirect.status()).toBe(303);
	expect(await redirect.headerValue('cache-control')).toContain('no-store');
	expect(await redirect.headerValue('referrer-policy')).toBe('no-referrer');
	const handoffUrl = await redirect.headerValue('location');
	expect(handoffUrl).toMatch(/\/auth\/callback#handoff=[^&]+/);
	await page.waitForURL((url) => url.pathname === '/settings');

	// The same tab, revisiting the URL from its history: the one-time verifier
	// was consumed by the first visit, so nothing is sent to the backend.
	await page.goto(handoffUrl!);
	await expect(page.getByRole('alert')).toContainText('expired or has already been used');
	await expect(page.getByRole('link', { name: 'Back to sign in' })).toBeVisible();
	expect(page.url()).not.toContain('handoff');

	// Another browser cannot redeem it either, even after planting a verifier
	// of its own: the backend only honors the one bound to the handoff.
	const context = await browser.newContext();
	try {
		const other = await context.newPage();
		await other.goto('/auth/login');
		await other.evaluate(() =>
			sessionStorage.setItem(
				'marimohub-oidc',
				JSON.stringify({ provider: 'e2e', verifier: 'x'.repeat(43), next: '/settings', created_at: Date.now() })
			)
		);

		const exchange = other.waitForResponse(
			(response) => response.url() === exchangeUrl && response.request().method() === 'POST'
		);
		await other.goto(handoffUrl!);
		expect((await exchange).status()).toBe(401);
		await expect(other.getByRole('alert')).toContainText('no longer valid');
		expect(await storedSession(other)).toBeNull();
	} finally {
		await context.close();
	}
});
