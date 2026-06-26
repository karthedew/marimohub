import { browser } from '$app/environment';
import type { User } from '$lib/api';
import { writable } from 'svelte/store';

export type AuthUser = Pick<User, 'username'> & Partial<User>;

type AuthState = {
	token: string | null;
	currentUser: AuthUser | null;
};

const storageKey = 'molab-auth';
const emptyAuthState: AuthState = { token: null, currentUser: null };

function userIdFromToken(token: string | null) {
	if (!browser || !token) return undefined;

	const payload = token.split('.')[1];
	if (!payload) return undefined;

	try {
		const normalized = payload.replace(/-/g, '+').replace(/_/g, '/');
		const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '=');
		const decoded = JSON.parse(atob(padded)) as { sub?: unknown };
		return typeof decoded.sub === 'string' && decoded.sub.length > 0 ? decoded.sub : undefined;
	} catch {
		return undefined;
	}
}

function withTokenIdentity(token: string | null, currentUser: AuthUser | null) {
	const id = userIdFromToken(token);
	if (!currentUser) return id ? { id, username: id } : null;
	return id ? { ...currentUser, id } : currentUser;
}

function initialState(): AuthState {
	if (!browser) return emptyAuthState;
	try {
		const stored = localStorage.getItem(storageKey);
		if (!stored) return emptyAuthState;
		const parsed = JSON.parse(stored) as AuthState;
		const token = parsed.token ?? null;
		return { token, currentUser: withTokenIdentity(token, parsed.currentUser ?? null) };
	} catch {
		localStorage.removeItem(storageKey);
		return emptyAuthState;
	}
}

function persistState(state: AuthState) {
	if (!browser) return;
	try {
		if (!state.token && !state.currentUser) {
			localStorage.removeItem(storageKey);
			return;
		}
		localStorage.setItem(storageKey, JSON.stringify(state));
	} catch {
		return;
	}
}

let authSnapshot = initialState();

function createAuthStore() {
	const store = writable<AuthState>(authSnapshot);

	store.subscribe((value) => {
		authSnapshot = value;
		persistState(value);
	});

	return {
		subscribe: store.subscribe,
		setSession: (token: string, currentUser: AuthUser | null) =>
			store.set({ token, currentUser: withTokenIdentity(token, currentUser) }),
		setToken: (token: string | null) => store.update((state) => ({ ...state, token })),
		setCurrentUser: (currentUser: AuthUser | null) => store.update((state) => ({ ...state, currentUser })),
		clear: () => store.set(emptyAuthState)
	};
}

export const auth = createAuthStore();

export function getAuthToken() {
	return authSnapshot.token;
}
