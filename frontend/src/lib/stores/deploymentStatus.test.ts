import { get } from 'svelte/store';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Deployment } from '$lib/api';

function deployment(overrides: Partial<Deployment> = {}): Deployment {
	return { slug: 'nb-1', status: 'running', url: 'http://localhost/api/deployments/nb-1', ...overrides };
}

function apiErrorClass() {
	return class ApiError extends Error {
		status: number;
		detail: string;
		constructor(status: number, detail: string) {
			super(detail);
			this.status = status;
			this.detail = detail;
		}
	};
}

beforeEach(() => {
	vi.resetModules();
});

describe('refresh', () => {
	it('moves through loading to ready with the fetched deployment', async () => {
		let resolveGet!: (value: unknown) => void;
		const getDeployment = vi.fn(() => new Promise((resolve) => (resolveGet = resolve)));
		vi.doMock('$lib/api', () => ({
			ApiError: apiErrorClass(),
			api: { notebooks: { getDeployment } }
		}));

		const { createDeploymentStatus } = await import('./deploymentStatus');
		const status = createDeploymentStatus('nb-1');
		const pending = status.refresh();

		expect(get(status)).toEqual({ status: 'loading', deployment: null });

		const value = deployment();
		resolveGet(value);
		await pending;

		expect(get(status)).toEqual({ status: 'ready', deployment: value });
		expect(getDeployment).toHaveBeenCalledWith('nb-1');
	});

	it('treats a 404 as no deployment, not a page-level error', async () => {
		const { ApiError } = { ApiError: apiErrorClass() };
		const getDeployment = vi.fn().mockRejectedValue(new ApiError(404, 'Deployment not found'));
		vi.doMock('$lib/api', () => ({ ApiError, api: { notebooks: { getDeployment } } }));

		const { createDeploymentStatus } = await import('./deploymentStatus');
		const status = createDeploymentStatus('nb-1');
		await status.refresh();

		expect(get(status)).toEqual({ status: 'none' });
	});

	it('preserves the last-known deployment across a transient failure', async () => {
		const { ApiError } = { ApiError: apiErrorClass() };
		const value = deployment();
		const getDeployment = vi.fn().mockResolvedValueOnce(value).mockRejectedValueOnce(new ApiError(503, 'Bad gateway'));
		vi.doMock('$lib/api', () => ({ ApiError, api: { notebooks: { getDeployment } } }));

		const { createDeploymentStatus } = await import('./deploymentStatus');
		const status = createDeploymentStatus('nb-1');

		await status.refresh();
		expect(get(status)).toEqual({ status: 'ready', deployment: value });

		await status.refresh();
		expect(get(status)).toEqual({ status: 'error', deployment: value, error: 'Bad gateway' });
	});

	it('preserves null last-known state across a transient failure before anything ever resolved', async () => {
		const { ApiError } = { ApiError: apiErrorClass() };
		const getDeployment = vi.fn().mockRejectedValue(new ApiError(500, 'Server exploded'));
		vi.doMock('$lib/api', () => ({ ApiError, api: { notebooks: { getDeployment } } }));

		const { createDeploymentStatus } = await import('./deploymentStatus');
		const status = createDeploymentStatus('nb-1');
		await status.refresh();

		expect(get(status)).toEqual({ status: 'error', deployment: null, error: 'Server exploded' });
	});

	it('discards a stale response that resolves after a newer refresh already settled', async () => {
		let resolveFirst!: (value: unknown) => void;
		let callCount = 0;
		const getDeployment = vi.fn(() => {
			callCount += 1;
			if (callCount === 1) return new Promise((resolve) => (resolveFirst = resolve));
			return Promise.resolve(deployment({ status: 'sleeping' }));
		});
		vi.doMock('$lib/api', () => ({ ApiError: apiErrorClass(), api: { notebooks: { getDeployment } } }));

		const { createDeploymentStatus } = await import('./deploymentStatus');
		const status = createDeploymentStatus('nb-1');

		const first = status.refresh();
		const second = status.refresh();
		await second;

		expect(get(status)).toEqual({ status: 'ready', deployment: deployment({ status: 'sleeping' }) });

		resolveFirst(deployment({ status: 'running' }));
		await first;

		// The stale first request resolving afterward must not overwrite what
		// the newer, already-settled refresh produced.
		expect(get(status)).toEqual({ status: 'ready', deployment: deployment({ status: 'sleeping' }) });
	});
});

describe('commit', () => {
	it('applies a mutation result immediately and discards a GET that was already in flight', async () => {
		let resolveGet!: (value: unknown) => void;
		const getDeployment = vi.fn(() => new Promise((resolve) => (resolveGet = resolve)));
		vi.doMock('$lib/api', () => ({ ApiError: apiErrorClass(), api: { notebooks: { getDeployment } } }));

		const { createDeploymentStatus } = await import('./deploymentStatus');
		const status = createDeploymentStatus('nb-1');
		const pending = status.refresh();

		const deployed = deployment({ status: 'sleeping' });
		status.commit(deployed);
		expect(get(status)).toEqual({ status: 'ready', deployment: deployed });

		// A GET issued just before the deploy call resolves afterward — it must
		// not clobber the authoritative state the deploy response already set.
		resolveGet(deployment({ status: 'stopped' }));
		await pending;
		expect(get(status)).toEqual({ status: 'ready', deployment: deployed });
	});

	it('commit(null) after a stop moves the store to none', async () => {
		vi.doMock('$lib/api', () => ({ ApiError: apiErrorClass(), api: { notebooks: { getDeployment: vi.fn() } } }));
		const { createDeploymentStatus } = await import('./deploymentStatus');
		const status = createDeploymentStatus('nb-1');

		status.commit(deployment());
		status.commit(null);

		expect(get(status)).toEqual({ status: 'none' });
	});
});

describe('activeDeployment / knownDeployment', () => {
	it('activeDeployment is null unless the store is ready with a non-stopped deployment', async () => {
		const { activeDeployment } = await import('./deploymentStatus');

		expect(activeDeployment({ status: 'loading', deployment: null })).toBeNull();
		expect(activeDeployment({ status: 'none' })).toBeNull();
		expect(activeDeployment({ status: 'ready', deployment: deployment({ status: 'stopped' }) })).toBeNull();
		expect(activeDeployment({ status: 'ready', deployment: deployment({ status: 'running' }) })).toEqual(
			deployment({ status: 'running' })
		);
	});

	it('knownDeployment returns the last confirmed row regardless of active/stopped', async () => {
		const { knownDeployment } = await import('./deploymentStatus');

		expect(knownDeployment({ status: 'none' })).toBeNull();
		expect(knownDeployment({ status: 'ready', deployment: deployment({ status: 'stopped' }) })).toEqual(
			deployment({ status: 'stopped' })
		);
		expect(knownDeployment({ status: 'error', deployment: deployment(), error: 'x' })).toEqual(deployment());
	});
});
