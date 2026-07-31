import { browser } from '$app/environment';
import { writable } from 'svelte/store';

import type { Workspace } from '$lib/api';

const STORAGE_KEY = 'marimohub-active-workspace';

function readStoredId(): string | null {
	if (!browser) return null;
	try {
		return localStorage.getItem(STORAGE_KEY);
	} catch {
		return null;
	}
}

let snapshot = readStoredId();

function createActiveWorkspaceStore() {
	const store = writable<string | null>(snapshot);

	function set(id: string | null) {
		snapshot = id;
		store.set(id);
		if (!browser) return;
		try {
			if (id) localStorage.setItem(STORAGE_KEY, id);
			else localStorage.removeItem(STORAGE_KEY);
		} catch {
			// Storage can be unavailable in privacy-restricted browser contexts.
		}
	}

	function reconcile(items: Workspace[]) {
		if (snapshot && items.some((workspace) => workspace.id === snapshot)) return snapshot;
		const next = items[0]?.id ?? null;
		set(next);
		return next;
	}

	return { subscribe: store.subscribe, set, reconcile, clear: () => set(null) };
}

export const activeWorkspace = createActiveWorkspaceStore();

export function getActiveWorkspaceId() {
	return snapshot;
}
