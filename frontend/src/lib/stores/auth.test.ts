import { get } from 'svelte/store';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { fakeJwt } from '../../test/fakeJwt';

vi.mock('$app/environment', () => ({ browser: true }));

const STORAGE_KEY = 'marimohub-auth';

beforeEach(() => {
	localStorage.clear();
	// Each test dynamically imports './auth' fresh so the module-level
	// snapshot it captures at load time reflects that test's own localStorage
	// state, rather than leaking session state set up by an earlier test.
	vi.resetModules();
});

describe('auth store', () => {
	it('derives the user id from the JWT subject rather than trusting caller-supplied data', async () => {
		const { auth, getAuthToken, isAuthenticated } = await import('./auth');
		auth.setSession(fakeJwt('user-42'), { username: 'ada' });

		expect(get(auth).currentUser).toEqual({ id: 'user-42', username: 'ada' });
		expect(getAuthToken()).not.toBeNull();
		expect(isAuthenticated()).toBe(true);
	});

	it('never produces a signed-in state from a token that yields no subject', async () => {
		const { auth, getAuthToken, isAuthenticated } = await import('./auth');
		auth.setSession('not-a-jwt', { username: 'ada' });

		expect(getAuthToken()).toBeNull();
		expect(get(auth).currentUser).toBeNull();
		expect(isAuthenticated()).toBe(false);
	});

	it('clears the session synchronously regardless of what triggered it', async () => {
		const { auth, getAuthToken } = await import('./auth');
		auth.setSession(fakeJwt('user-1'));
		expect(getAuthToken()).not.toBeNull();

		auth.clear();
		expect(getAuthToken()).toBeNull();
		expect(get(auth)).toEqual({ token: null, currentUser: null });
	});
});

describe('invalid stored auth removal', () => {
	it('discards a stored session whose token cannot be validated', async () => {
		localStorage.setItem(STORAGE_KEY, JSON.stringify({ token: 'garbage-token', currentUser: { username: 'ada' } }));

		const { getAuthToken } = await import('./auth');

		expect(getAuthToken()).toBeNull();
		expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
	});

	it('discards storage that is not valid JSON', async () => {
		localStorage.setItem(STORAGE_KEY, '{not json');

		const { getAuthToken } = await import('./auth');

		expect(getAuthToken()).toBeNull();
		expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
	});

	it('restores a validly stored session as-is', async () => {
		const token = fakeJwt('user-7');
		localStorage.setItem(STORAGE_KEY, JSON.stringify({ token, currentUser: { username: 'bea' } }));

		const { getAuthToken, isAuthenticated } = await import('./auth');

		expect(getAuthToken()).toBe(token);
		expect(isAuthenticated()).toBe(true);
	});
});
