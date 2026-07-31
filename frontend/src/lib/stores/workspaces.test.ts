import { get } from 'svelte/store';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { fakeJwt } from '../../test/fakeJwt';

vi.mock('$app/environment', () => ({ browser: true }));

function workspace(id: string, role: 'owner' | 'editor' | 'viewer' = 'owner') {
	return { id, slug: id, name: id, role, created_at: '2026-01-01T00:00:00Z' };
}

beforeEach(() => {
	localStorage.clear();
	// Every test re-imports both modules so the auth snapshot and the
	// workspace store's internal generation counter start clean instead of
	// leaking state set up by an earlier test in this file.
	vi.resetModules();
});

describe('refresh', () => {
	it('never calls the API while anonymous and settles on the anonymous state', async () => {
		const list = vi.fn();
		vi.doMock('$lib/api', () => ({
			ApiError: class extends Error {},
			api: { workspaces: { list } }
		}));

		const { workspaces } = await import('./workspaces');
		await workspaces.refresh();

		expect(list).not.toHaveBeenCalled();
		expect(get(workspaces)).toEqual({ status: 'anonymous', items: [] });
	});

	it('moves through loading to ready with the fetched items', async () => {
		const { auth } = await import('$lib/stores/auth');
		auth.setSession(fakeJwt('user-1'));

		let resolveList!: (items: unknown[]) => void;
		const list = vi.fn(() => new Promise((resolve) => (resolveList = resolve)));
		vi.doMock('$lib/api', () => ({
			ApiError: class extends Error {},
			api: { workspaces: { list } }
		}));

		const { workspaces } = await import('./workspaces');
		const pending = workspaces.refresh();

		expect(get(workspaces).status).toBe('loading');

		const items = [workspace('w1')];
		resolveList(items);
		await pending;

		expect(get(workspaces)).toEqual({ status: 'ready', items });
	});

	it('reports a retryable error and preserves the last-known items', async () => {
		const { auth } = await import('$lib/stores/auth');
		auth.setSession(fakeJwt('user-1'));

		const items = [workspace('w1')];
		const list = vi.fn().mockResolvedValueOnce(items);
		vi.doMock('$lib/api', () => ({
			ApiError: class ApiError extends Error {
				status: number;
				detail: string;
				constructor(status: number, detail: string) {
					super(detail);
					this.status = status;
					this.detail = detail;
				}
			},
			api: { workspaces: { list } }
		}));

		const { workspaces } = await import('./workspaces');
		const { ApiError } = await import('$lib/api');

		await workspaces.refresh();
		expect(get(workspaces)).toEqual({ status: 'ready', items });

		list.mockRejectedValueOnce(new ApiError(500, 'Server exploded'));
		await workspaces.refresh();
		expect(get(workspaces)).toEqual({ status: 'error', items, error: 'Server exploded' });

		// Retrying should be able to recover back to ready.
		list.mockResolvedValueOnce(items);
		await workspaces.refresh();
		expect(get(workspaces)).toEqual({ status: 'ready', items });
	});

	it('suppresses a stale response that resolves after a newer refresh started', async () => {
		const { auth } = await import('$lib/stores/auth');
		auth.setSession(fakeJwt('user-1'));

		let resolveFirst!: (items: unknown[]) => void;
		let callCount = 0;
		const list = vi.fn(() => {
			callCount += 1;
			if (callCount === 1) return new Promise((resolve) => (resolveFirst = resolve));
			return Promise.resolve([workspace('w2')]);
		});
		vi.doMock('$lib/api', () => ({
			ApiError: class extends Error {},
			api: { workspaces: { list } }
		}));

		const { workspaces } = await import('./workspaces');
		const first = workspaces.refresh();
		const second = workspaces.refresh();
		await second;

		expect(get(workspaces)).toEqual({ status: 'ready', items: [workspace('w2')] });

		// The stale first request resolving afterward must not overwrite the
		// state the newer, already-settled refresh produced.
		resolveFirst([workspace('w1')]);
		await first;
		expect(get(workspaces)).toEqual({ status: 'ready', items: [workspace('w2')] });
	});

	it('suppresses a response that resolves after the token changed underneath it', async () => {
		const { auth } = await import('$lib/stores/auth');
		auth.setSession(fakeJwt('user-1'));

		let resolveList!: (items: unknown[]) => void;
		const list = vi.fn(() => new Promise((resolve) => (resolveList = resolve)));
		vi.doMock('$lib/api', () => ({
			ApiError: class extends Error {},
			api: { workspaces: { list } }
		}));

		const { workspaces } = await import('./workspaces');
		const pending = workspaces.refresh();

		// A token change (e.g. a different user signing in while this request
		// was in flight) means the resolved items belong to nobody currently
		// active. The token guard must drop them rather than let the store
		// briefly render another session's workspaces.
		auth.clear();
		auth.setSession(fakeJwt('user-2'));
		resolveList([workspace('w1')]);
		await pending;

		expect(get(workspaces).items).toEqual([]);
	});
});

describe('clear', () => {
	it('resets to anonymous and invalidates any in-flight refresh', async () => {
		const { auth } = await import('$lib/stores/auth');
		auth.setSession(fakeJwt('user-1'));

		let resolveList!: (items: unknown[]) => void;
		const list = vi.fn(() => new Promise((resolve) => (resolveList = resolve)));
		vi.doMock('$lib/api', () => ({
			ApiError: class extends Error {},
			api: { workspaces: { list } }
		}));

		const { workspaces } = await import('./workspaces');
		const pending = workspaces.refresh();

		workspaces.clear();
		expect(get(workspaces)).toEqual({ status: 'anonymous', items: [] });

		resolveList([workspace('w1')]);
		await pending;
		expect(get(workspaces)).toEqual({ status: 'anonymous', items: [] });
	});
});

describe('roleFor / canWrite / writableWorkspaces', () => {
	it('derives the caller role, write capability, and the writable subset', async () => {
		vi.doMock('$lib/api', () => ({ ApiError: class extends Error {}, api: { workspaces: { list: vi.fn() } } }));
		const { roleFor, canWrite, writableWorkspaces } = await import('./workspaces');

		const items = [workspace('w1', 'owner'), workspace('w2', 'editor'), workspace('w3', 'viewer')];

		expect(roleFor(items, 'w1')).toBe('owner');
		expect(roleFor(items, 'w3')).toBe('viewer');
		expect(roleFor(items, 'missing')).toBeNull();

		expect(canWrite(items, 'w1')).toBe(true);
		expect(canWrite(items, 'w2')).toBe(true);
		expect(canWrite(items, 'w3')).toBe(false);
		expect(canWrite(items, 'missing')).toBe(false);

		expect(writableWorkspaces(items)).toEqual([items[0], items[1]]);
	});
});
