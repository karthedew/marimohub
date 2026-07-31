import { writable } from 'svelte/store';

import { ApiError, api, type Workspace, type WorkspaceRole } from '$lib/api';
import { getAuthToken } from '$lib/stores/auth';

export type WorkspaceState =
	| { status: 'anonymous'; items: [] }
	| { status: 'loading'; items: Workspace[] }
	| { status: 'ready'; items: Workspace[] }
	| { status: 'error'; items: Workspace[]; error: string };

const ANONYMOUS: WorkspaceState = { status: 'anonymous', items: [] };

function createWorkspaceStore() {
	const store = writable<WorkspaceState>(ANONYMOUS);

	// Bumped by every refresh and by clear(). A response only commits if it is
	// still the most recent request AND the token it was issued under is still
	// the active one — either check alone misses a logout/login race that
	// lands inside the same in-flight window.
	let generation = 0;

	async function refresh() {
		const token = getAuthToken();
		if (!token) {
			generation++;
			store.set(ANONYMOUS);
			return;
		}

		const request = ++generation;
		let previousItems: Workspace[] = [];
		store.update((state) => {
			previousItems = state.items;
			return { status: 'loading', items: previousItems };
		});

		try {
			const items = await api.workspaces.list();
			if (request !== generation || token !== getAuthToken()) return;
			store.set({ status: 'ready', items });
		} catch (caught) {
			if (request !== generation || token !== getAuthToken()) return;
			const message = caught instanceof ApiError ? caught.detail : 'Unable to load workspaces.';
			store.set({ status: 'error', items: previousItems, error: message });
		}
	}

	function clear() {
		generation++;
		store.set(ANONYMOUS);
	}

	return { subscribe: store.subscribe, refresh, clear };
}

export const workspaces = createWorkspaceStore();

export function roleFor(items: Workspace[], workspaceId: string): WorkspaceRole | null {
	return items.find((item) => item.id === workspaceId)?.role ?? null;
}

export function canWrite(items: Workspace[], workspaceId: string): boolean {
	const role = roleFor(items, workspaceId);
	return role === 'owner' || role === 'editor';
}

export function writableWorkspaces(items: Workspace[]): Workspace[] {
	return items.filter((item) => item.role === 'owner' || item.role === 'editor');
}
