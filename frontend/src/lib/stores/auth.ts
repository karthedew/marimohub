import { browser } from '$app/environment';
import { writable } from 'svelte/store';
import type { User } from '$lib/api';

type AuthState = {
	token: string | null;
	currentUser: User | null;
};

const storageKey = 'molab-auth';

function initialState(): AuthState {
	if (!browser) return { token: null, currentUser: null };
	const stored = localStorage.getItem(storageKey);
	if (!stored) return { token: null, currentUser: null };
	try {
		const parsed = JSON.parse(stored) as AuthState;
		return { token: parsed.token ?? null, currentUser: parsed.currentUser ?? null };
	} catch {
		localStorage.removeItem(storageKey);
		return { token: null, currentUser: null };
	}
}

let authSnapshot = initialState();

function createAuthStore() {
	const store = writable<AuthState>(authSnapshot);

	store.subscribe((value) => {
		authSnapshot = value;
		if (browser) localStorage.setItem(storageKey, JSON.stringify(value));
	});

	return {
		subscribe: store.subscribe,
		setToken: (token: string | null) => store.update((state) => ({ ...state, token })),
		setCurrentUser: (currentUser: User | null) => store.update((state) => ({ ...state, currentUser })),
		clear: () => store.set({ token: null, currentUser: null })
	};
}

export const auth = createAuthStore();

export function getAuthToken() {
	return authSnapshot.token;
}
