import { browser } from '$app/environment';
import { writable } from 'svelte/store';

// There is no "get current user" endpoint, so identity beyond the JWT subject
// is only ever what sign-in handed back in the moment (the account register or
// a provider sign-in returned, or the username typed at login) — never
// re-fetched, never verified again client-side.
export type AuthUser = {
	id: string;
	username: string;
	email?: string;
	created_at?: string;
};

type AuthState = {
	token: string | null;
	currentUser: AuthUser | null;
};

const STORAGE_KEY = 'marimohub-auth';
const SIGNED_OUT: AuthState = { token: null, currentUser: null };

// Decodes the JWT `sub` claim for display and client-side identity checks
// only (e.g. "is this my notebook") — it is never a substitute for backend
// authorization, which re-verifies the token's signature on every request.
function subjectFromToken(token: string): string | null {
	const payload = token.split('.')[1];
	if (!payload) return null;
	try {
		const normalized = payload.replace(/-/g, '+').replace(/_/g, '/');
		const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '=');
		const decoded = JSON.parse(atob(padded)) as { sub?: unknown };
		return typeof decoded.sub === 'string' && decoded.sub.length > 0 ? decoded.sub : null;
	} catch {
		return null;
	}
}

// A session is only ever `{token, currentUser}` with a currentUser.id derived
// from the token itself. A token that cannot yield an id is not a usable
// session, so this always resolves to fully signed-in or fully signed-out —
// never a half-authenticated state that callers would have to special-case.
function deriveState(token: string | null, partialUser: Partial<AuthUser> | null): AuthState {
	if (!token) return SIGNED_OUT;
	const id = subjectFromToken(token);
	if (!id) return SIGNED_OUT;
	return { token, currentUser: { ...partialUser, id, username: partialUser?.username ?? id } };
}

function readStoredState(): AuthState {
	if (!browser) return SIGNED_OUT;
	const raw = localStorage.getItem(STORAGE_KEY);
	if (!raw) return SIGNED_OUT;

	try {
		const parsed = JSON.parse(raw) as Partial<AuthState>;
		const state = deriveState(parsed.token ?? null, parsed.currentUser ?? null);
		if (state === SIGNED_OUT && parsed.token) localStorage.removeItem(STORAGE_KEY);
		return state;
	} catch {
		localStorage.removeItem(STORAGE_KEY);
		return SIGNED_OUT;
	}
}

function persistState(state: AuthState) {
	if (!browser) return;
	if (!state.token) {
		localStorage.removeItem(STORAGE_KEY);
		return;
	}
	localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}

let snapshot = readStoredState();

// Storage is written outside the store's subscriber: svelte/store runs every
// subscriber from one queue shared by all stores, so a throw from inside one
// (localStorage over quota, say) would stop every store in the app updating.
function createAuthStore() {
	const store = writable<AuthState>(snapshot);

	store.subscribe((value) => {
		snapshot = value;
	});

	return {
		subscribe: store.subscribe,
		// Stored before it is published, so a session the browser refuses to
		// store throws with nothing changed rather than half signed in.
		setSession: (token: string, user: Partial<AuthUser> | null = null) => {
			const state = deriveState(token, user);
			persistState(state);
			store.set(state);
		},
		clear: () => {
			store.set(SIGNED_OUT);
			persistState(SIGNED_OUT);
		}
	};
}

export const auth = createAuthStore();

// Synchronous snapshots for call sites that can't subscribe to the store:
// the API client (every outgoing request needs the current token) and
// client-only route guards (`+page.ts` load functions run once, not reactively).
export function getAuthToken() {
	return snapshot.token;
}

export function isAuthenticated() {
	return snapshot.token !== null && snapshot.currentUser !== null;
}
