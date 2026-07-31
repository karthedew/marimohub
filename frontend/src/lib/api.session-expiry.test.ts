import { beforeEach, describe, expect, it, vi } from 'vitest';

import { fakeJwt } from '../test/fakeJwt';

const gotoMock = vi.fn();

// The centralized 401 handler only redirects in the browser; mocking these
// lets this file exercise that browser path deterministically instead of
// relying on Vitest's default (non-browser) `$app/environment` value.
vi.mock('$app/environment', () => ({ browser: true }));
vi.mock('$app/navigation', () => ({ goto: gotoMock }));

const { apiWithFetch } = await import('./api');
const { auth, getAuthToken } = await import('./stores/auth');

function unauthorized() {
	return new Response(JSON.stringify({ detail: 'Invalid or expired token' }), {
		status: 401,
		headers: { 'Content-Type': 'application/json' }
	});
}

beforeEach(() => {
	gotoMock.mockClear();
	auth.clear();
	window.history.pushState({}, '', '/notebooks/abc?tab=code');
});

describe('expired session handling', () => {
	it('clears the local session and redirects to login when a token-bearing request gets a 401', async () => {
		auth.setSession(fakeJwt('user-1'));
		expect(getAuthToken()).not.toBeNull();

		const fetcher = vi.fn().mockResolvedValue(unauthorized());
		await expect(apiWithFetch(fetcher).notebooks.list()).rejects.toMatchObject({ status: 401 });

		expect(getAuthToken()).toBeNull();
		expect(gotoMock).toHaveBeenCalledWith('/auth/login?next=%2Fnotebooks%2Fabc%3Ftab%3Dcode');
	});

	it('leaves an anonymous request alone on a 401 — there is no session to expire', async () => {
		expect(getAuthToken()).toBeNull();

		const fetcher = vi.fn().mockResolvedValue(unauthorized());
		await expect(apiWithFetch(fetcher).notebooks.get('nb-1')).rejects.toMatchObject({ status: 401 });

		expect(getAuthToken()).toBeNull();
		expect(gotoMock).not.toHaveBeenCalled();
	});

	it('does not redirect on a 401 from a request that opted out of the centralized handler', async () => {
		auth.setSession(fakeJwt('user-1'));

		const fetcher = vi.fn().mockResolvedValue(unauthorized());
		await expect(apiWithFetch(fetcher).auth.logout()).rejects.toMatchObject({ status: 401 });

		// `handle401: false` on logout means this 401 must not trigger the
		// generic redirect — logout owns clearing its own state instead.
		expect(getAuthToken()).not.toBeNull();
		expect(gotoMock).not.toHaveBeenCalled();
	});
});
