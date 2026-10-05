import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';

// `$lib/api` reads PUBLIC_API_URL once, when the module loads, so this file
// sets it first and imports a fresh copy of the module.
describe('oidcLoginUrl with a separate backend origin (compose)', () => {
	beforeAll(() => {
		globalThis.__sveltekit_dev!.env.PUBLIC_API_URL = 'http://localhost:8000/';
		vi.resetModules();
	});

	afterAll(() => {
		delete globalThis.__sveltekit_dev!.env.PUBLIC_API_URL;
		vi.resetModules();
	});

	it('is an absolute URL on the backend origin, so a full-page navigation leaves the SPA', async () => {
		const { oidcLoginUrl } = await import('./api');
		expect(oidcLoginUrl('google', 'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM')).toBe(
			'http://localhost:8000/api/auth/oidc/google/login?challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM'
		);
	});
});
