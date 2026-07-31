import { writable } from 'svelte/store';

import { ApiError, api, type Deployment } from '$lib/api';

export type DeploymentStatusState =
	| { status: 'loading'; deployment: Deployment | null }
	| { status: 'ready'; deployment: Deployment }
	| { status: 'none' }
	| { status: 'error'; deployment: Deployment | null; error: string };

const UNKNOWN: DeploymentStatusState = { status: 'loading', deployment: null };

/**
 * One notebook's deployment read-state: a small, independently testable
 * store instead of page-local `$state`, so the revision guard below has the
 * same coverage the workspace store gets.
 *
 * A fresh instance belongs to exactly one notebook id; callers that navigate
 * between notebooks without remounting must construct a new instance rather
 * than reuse one across ids.
 */
export function createDeploymentStatus(notebookId: string) {
	const store = writable<DeploymentStatusState>(UNKNOWN);

	// Bumped by every `refresh()` and by `commit()`. A GET response only
	// applies if it is still the most recent request in flight — this is what
	// stops a slow, superseded response (e.g. one issued just before a deploy
	// or stop mutation resolves) from clobbering newer, authoritative state.
	let generation = 0;

	function lastKnown(state: DeploymentStatusState): Deployment | null {
		return state.status === 'none' ? null : state.deployment;
	}

	async function refresh() {
		const request = ++generation;
		let previous: Deployment | null = null;
		store.update((state) => {
			previous = lastKnown(state);
			return { status: 'loading', deployment: previous };
		});

		try {
			const deployment = await api.notebooks.getDeployment(notebookId);
			if (request !== generation) return;
			store.set({ status: 'ready', deployment });
		} catch (caught) {
			if (request !== generation) return;
			if (caught instanceof ApiError && caught.status === 404) {
				store.set({ status: 'none' });
				return;
			}
			const message = caught instanceof ApiError ? caught.detail : 'Unable to load deployment status.';
			store.set({ status: 'error', deployment: previous, error: message });
		}
	}

	// Commits the authoritative result of a deploy/stop mutation. Bumping the
	// generation here, not just on `refresh()`, is what keeps a GET issued
	// right before the mutation from landing after it and overwriting it.
	function commit(deployment: Deployment | null) {
		generation++;
		store.set(deployment ? { status: 'ready', deployment } : { status: 'none' });
	}

	return { subscribe: store.subscribe, refresh, commit };
}

export function activeDeployment(state: DeploymentStatusState): Deployment | null {
	return state.status === 'ready' && state.deployment.status !== 'stopped' ? state.deployment : null;
}

export function knownDeployment(state: DeploymentStatusState): Deployment | null {
	return state.status === 'none' ? null : state.deployment;
}
