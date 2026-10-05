// Browser half of signing in with an external identity provider.
//
// The SPA never sees the provider's tokens. The backend runs the whole OIDC
// authorization-code flow and finally redirects back to `/auth/callback` with
// a short-lived *handoff* in the URL fragment. A handoff alone is worthless:
// before leaving, the tab that starts sign-in keeps a random verifier in
// sessionStorage and sends only its S256 challenge to the backend, which binds
// the handoff to that challenge. Only the same tab, holding the verifier, can
// exchange the handoff for a session — so a handoff URL leaked from history,
// logs, or a forced redirect (login CSRF) cannot sign anyone in.
//
// This module holds that verifier record plus the provider-facing copy. It has
// no network or router dependencies so every rule here is unit-testable.
import type { AuthProvider } from './api';

export const OIDC_ATTEMPT_STORAGE_KEY = 'marimohub-oidc';

// Matches the lifetime of the backend's login cookie: an attempt older than
// this could not complete anyway.
export const OIDC_ATTEMPT_TTL_MS = 10 * 60 * 1000;

// Tolerates a small backwards clock adjustment between start and callback.
const CLOCK_SKEW_MS = 60 * 1000;

// RFC 7636 §4.1: 43–128 unreserved characters. Ours are always 43.
const VERIFIER_PATTERN = /^[A-Za-z0-9\-._~]{43,128}$/;

export type OidcAttempt = {
	provider: string;
	verifier: string;
	next: string;
	created_at: number;
};

type CryptoLike = Pick<Crypto, 'getRandomValues' | 'subtle'>;
type StorageLike = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

type AttemptOptions = {
	crypto?: CryptoLike;
	storage?: StorageLike;
	now?: () => number;
};

// Thrown when this browser context cannot run provider sign-in at all. The
// message is written for the person at the keyboard.
export class OidcUnavailableError extends Error {
	constructor(message: string, options?: ErrorOptions) {
		super(message, options);
		this.name = 'OidcUnavailableError';
	}
}

export function base64UrlEncode(bytes: Uint8Array): string {
	let binary = '';
	for (const byte of bytes) binary += String.fromCharCode(byte);
	return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function defaultCrypto(): CryptoLike {
	const crypto = globalThis.crypto;
	// SubtleCrypto only exists in secure contexts: https, or http://localhost.
	if (!crypto?.subtle || typeof crypto.getRandomValues !== 'function') {
		throw new OidcUnavailableError('Signing in with an identity provider needs a secure (HTTPS) connection.');
	}
	return crypto;
}

function defaultStorage(): StorageLike {
	return window.sessionStorage;
}

// 32 random bytes, base64url without padding: always 43 characters.
export function createVerifier(crypto: CryptoLike = defaultCrypto()): string {
	return base64UrlEncode(crypto.getRandomValues(new Uint8Array(32)));
}

// BASE64URL(SHA-256(ASCII(verifier))), the S256 method from RFC 7636 §4.2.
export async function challengeFor(verifier: string, crypto: CryptoLike = defaultCrypto()): Promise<string> {
	const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
	return base64UrlEncode(new Uint8Array(digest));
}

/**
 * Records a new sign-in attempt for this tab and returns the challenge to send
 * to the backend's login route. Starting again replaces any earlier attempt.
 * `next` must already be a safe path; the callback re-checks it regardless.
 */
export async function startOidcAttempt(
	{ provider, next }: { provider: string; next: string },
	{ crypto, storage, now = Date.now }: AttemptOptions = {}
): Promise<string> {
	const cryptoImpl = crypto ?? defaultCrypto();
	const verifier = createVerifier(cryptoImpl);
	const challenge = await challengeFor(verifier, cryptoImpl);
	const attempt: OidcAttempt = { provider, verifier, next, created_at: now() };

	try {
		(storage ?? defaultStorage()).setItem(OIDC_ATTEMPT_STORAGE_KEY, JSON.stringify(attempt));
	} catch (error) {
		throw new OidcUnavailableError(
			'This browser is blocking the site storage that signing in with an identity provider needs. Allow storage for this site and try again.',
			{ cause: error }
		);
	}
	return challenge;
}

/**
 * Reads and always deletes this tab's sign-in attempt, so a verifier can be
 * used at most once. Returns null when there is none, or when it is malformed
 * or older than {@link OIDC_ATTEMPT_TTL_MS}.
 */
export function consumeOidcAttempt({ storage, now = Date.now }: Omit<AttemptOptions, 'crypto'> = {}): OidcAttempt | null {
	let store: StorageLike;
	try {
		store = storage ?? defaultStorage();
	} catch {
		return null;
	}

	let raw: string | null;
	try {
		raw = store.getItem(OIDC_ATTEMPT_STORAGE_KEY);
	} catch {
		raw = null;
	}
	try {
		store.removeItem(OIDC_ATTEMPT_STORAGE_KEY);
	} catch {
		// Nothing more can be done; an unreadable record is ignored below anyway.
	}

	return parseAttempt(raw, now());
}

function parseAttempt(raw: string | null, now: number): OidcAttempt | null {
	if (!raw) return null;

	let value: unknown;
	try {
		value = JSON.parse(raw);
	} catch {
		return null;
	}
	if (!value || typeof value !== 'object') return null;

	const { provider, verifier, next, created_at } = value as Record<string, unknown>;
	if (typeof provider !== 'string' || provider.length === 0) return null;
	if (typeof verifier !== 'string' || !VERIFIER_PATTERN.test(verifier)) return null;
	if (typeof next !== 'string') return null;
	if (typeof created_at !== 'number' || !Number.isFinite(created_at)) return null;

	const age = now - created_at;
	if (age > OIDC_ATTEMPT_TTL_MS || age < -CLOCK_SKEW_MS) return null;

	return { provider, verifier, next, created_at };
}

export type ProviderSignInMode = 'login' | 'register';

// Google's branding guidelines allow only their own wording on a Google
// button; every other provider gets the same neutral label.
export function providerButtonLabel(provider: Pick<AuthProvider, 'kind' | 'display_name'>, mode: ProviderSignInMode) {
	if (provider.kind === 'google') return mode === 'login' ? 'Sign in with Google' : 'Continue with Google';
	return `Continue with ${provider.display_name}`;
}

// The backend only accepts DNS-label slugs, which also keeps a slug from ever
// being a dot segment (`..`) inside the login URL's path.
const PROVIDER_SLUG_PATTERN = /^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$/;

function isAuthProvider(value: unknown): value is AuthProvider {
	if (!value || typeof value !== 'object') return false;
	const { slug, display_name, kind } = value as Record<string, unknown>;
	return (
		typeof slug === 'string' &&
		PROVIDER_SLUG_PATTERN.test(slug) &&
		typeof display_name === 'string' &&
		display_name.trim().length > 0 &&
		typeof kind === 'string'
	);
}

/**
 * The providers to offer as buttons, from whatever `/api/auth/providers`
 * returned: malformed entries are dropped, and Google comes first because its
 * guidelines require its button to be at least as prominent as any other.
 */
export function signInProviders(value: unknown): AuthProvider[] {
	if (!Array.isArray(value)) return [];
	const providers = value.filter(isAuthProvider);
	return [...providers.filter((p) => p.kind === 'google'), ...providers.filter((p) => p.kind !== 'google')];
}

export const OIDC_ERROR_CODES = [
	'oidc_denied',
	'oidc_expired',
	'oidc_failed',
	'oidc_account_exists',
	'oidc_domain_not_allowed',
	'oidc_invalid_request'
] as const;

export type OidcErrorCode = (typeof OIDC_ERROR_CODES)[number];

const OIDC_ERROR_MESSAGES: Record<OidcErrorCode, string> = {
	oidc_denied:
		'Sign-in was cancelled at your identity provider, so you have not been signed in. Try again, or use your username and password.',
	oidc_expired: 'That sign-in attempt expired or could not be matched to this browser. Please try again.',
	oidc_failed: 'We could not complete sign-in with your identity provider. Please try again in a moment.',
	// Worded for an account with a password and one made through another
	// provider alike, without revealing which kind it is.
	oidc_account_exists:
		'An account with this email address already exists, and it cannot be linked to this identity provider. Sign in the way you did before: with your username and password, or with the identity provider you signed up with.',
	oidc_domain_not_allowed:
		'That account’s organization is not allowed to sign in here. Choose an account from an allowed domain.',
	oidc_invalid_request: 'The sign-in request was not valid. Please start again from this page.'
};

const UNKNOWN_OIDC_ERROR_MESSAGE = 'Sign-in with your identity provider failed. Please try again.';

// The message for the backend's `?error=` code on `/auth/login`, or null when
// there is none. Unknown codes still get a generic message rather than none.
export function oidcErrorMessage(code: string | null | undefined): string | null {
	if (!code) return null;
	return Object.hasOwn(OIDC_ERROR_MESSAGES, code)
		? OIDC_ERROR_MESSAGES[code as OidcErrorCode]
		: UNKNOWN_OIDC_ERROR_MESSAGE;
}
