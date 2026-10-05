import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
	OIDC_ATTEMPT_STORAGE_KEY,
	OIDC_ATTEMPT_TTL_MS,
	OIDC_ERROR_CODES,
	OidcUnavailableError,
	base64UrlEncode,
	challengeFor,
	consumeOidcAttempt,
	createVerifier,
	oidcErrorMessage,
	providerButtonLabel,
	signInProviders,
	startOidcAttempt
} from './oidc';

const BASE64URL_43 = /^[A-Za-z0-9_-]{43}$/;
const NOW = 1_790_000_000_000;

function storedAttempt() {
	const raw = sessionStorage.getItem(OIDC_ATTEMPT_STORAGE_KEY);
	return raw ? (JSON.parse(raw) as Record<string, unknown>) : null;
}

function storeAttempt(value: unknown) {
	sessionStorage.setItem(OIDC_ATTEMPT_STORAGE_KEY, typeof value === 'string' ? value : JSON.stringify(value));
}

function validAttempt(overrides: Record<string, unknown> = {}) {
	return { provider: 'google', verifier: 'v'.repeat(43), next: '/notebooks/1', created_at: NOW, ...overrides };
}

beforeEach(() => {
	sessionStorage.clear();
});

afterEach(() => {
	vi.unstubAllGlobals();
});

describe('PKCE-style verifier and challenge', () => {
	it('encodes base64url without padding', () => {
		expect(base64UrlEncode(new Uint8Array([0xfb, 0xff]))).toBe('-_8');
		expect(base64UrlEncode(new Uint8Array([]))).toBe('');
	});

	it('creates a 43-character base64url verifier from 32 random bytes', () => {
		const first = createVerifier();
		const second = createVerifier();
		expect(first).toMatch(BASE64URL_43);
		expect(second).toMatch(BASE64URL_43);
		expect(first).not.toBe(second);
	});

	it('draws the verifier from crypto.getRandomValues', () => {
		const getRandomValues = vi.fn(<T extends ArrayBufferView | null>(array: T) => array);
		const verifier = createVerifier({ getRandomValues, subtle: crypto.subtle } as unknown as Crypto);
		expect(getRandomValues).toHaveBeenCalledTimes(1);
		expect((getRandomValues.mock.calls[0][0] as Uint8Array).length).toBe(32);
		expect(verifier).toBe('A'.repeat(43));
	});

	it('derives the S256 challenge exactly as RFC 7636 Appendix B does', async () => {
		await expect(challengeFor('dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk')).resolves.toBe(
			'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM'
		);
	});
});

describe('startOidcAttempt', () => {
	it('stores exactly {provider, verifier, next, created_at} and returns the matching challenge', async () => {
		const challenge = await startOidcAttempt({ provider: 'google', next: '/workspaces' }, { now: () => NOW });

		const attempt = storedAttempt();
		expect(Object.keys(attempt ?? {}).sort()).toEqual(['created_at', 'next', 'provider', 'verifier']);
		expect(attempt).toMatchObject({ provider: 'google', next: '/workspaces', created_at: NOW });
		expect(attempt?.verifier).toMatch(BASE64URL_43);
		expect(challenge).toMatch(BASE64URL_43);
		expect(challenge).toBe(await challengeFor(String(attempt?.verifier)));
	});

	it('replaces an earlier attempt from the same tab', async () => {
		await startOidcAttempt({ provider: 'google', next: '/a' });
		const first = storedAttempt();
		await startOidcAttempt({ provider: 'e2e', next: '/b' });
		const second = storedAttempt();

		expect(second).toMatchObject({ provider: 'e2e', next: '/b' });
		expect(second?.verifier).not.toBe(first?.verifier);
	});

	it('explains, rather than crashes, when site storage is blocked', async () => {
		const storage = {
			getItem: () => null,
			removeItem: () => {},
			setItem: () => {
				throw new DOMException('denied', 'SecurityError');
			}
		};
		await expect(startOidcAttempt({ provider: 'google', next: '/' }, { storage })).rejects.toBeInstanceOf(
			OidcUnavailableError
		);
	});

	it('explains, rather than crashes, outside a secure context (no SubtleCrypto)', async () => {
		vi.stubGlobal('crypto', { getRandomValues: crypto.getRandomValues.bind(crypto) });
		await expect(startOidcAttempt({ provider: 'google', next: '/' })).rejects.toThrow(/secure \(HTTPS\) connection/);
		expect(storedAttempt()).toBeNull();
	});
});

describe('consumeOidcAttempt', () => {
	it('returns the stored attempt once and deletes it', () => {
		storeAttempt(validAttempt());

		expect(consumeOidcAttempt({ now: () => NOW + 1_000 })).toEqual(validAttempt());
		expect(sessionStorage.getItem(OIDC_ATTEMPT_STORAGE_KEY)).toBeNull();
		expect(consumeOidcAttempt({ now: () => NOW + 1_000 })).toBeNull();
	});

	it('round-trips an attempt written by startOidcAttempt', async () => {
		const challenge = await startOidcAttempt({ provider: 'e2e', next: '/settings' }, { now: () => NOW });
		const attempt = consumeOidcAttempt({ now: () => NOW });

		expect(attempt).toMatchObject({ provider: 'e2e', next: '/settings', created_at: NOW });
		expect(await challengeFor(attempt!.verifier)).toBe(challenge);
	});

	it('accepts an attempt that is exactly ten minutes old', () => {
		storeAttempt(validAttempt());
		expect(consumeOidcAttempt({ now: () => NOW + OIDC_ATTEMPT_TTL_MS })).not.toBeNull();
	});

	it('rejects and deletes an attempt older than ten minutes', () => {
		storeAttempt(validAttempt());
		expect(consumeOidcAttempt({ now: () => NOW + OIDC_ATTEMPT_TTL_MS + 1 })).toBeNull();
		expect(sessionStorage.getItem(OIDC_ATTEMPT_STORAGE_KEY)).toBeNull();
	});

	it('rejects an attempt dated well into the future', () => {
		storeAttempt(validAttempt({ created_at: NOW + 5 * 60 * 1000 }));
		expect(consumeOidcAttempt({ now: () => NOW })).toBeNull();
	});

	it.each([
		['not JSON', '{oops'],
		['a JSON string', '"hello"'],
		['null', 'null'],
		['a missing provider', validAttempt({ provider: undefined })],
		['an empty provider', validAttempt({ provider: '' })],
		['a short verifier', validAttempt({ verifier: 'abc' })],
		['a verifier with forbidden characters', validAttempt({ verifier: `${'v'.repeat(42)}/` })],
		['a non-string next', validAttempt({ next: 42 })],
		['a string timestamp', validAttempt({ created_at: String(NOW) })],
		['a non-finite timestamp', '{"provider":"g","verifier":"' + 'v'.repeat(43) + '","next":"/","created_at":1e999}']
	])('discards a record with %s, and still deletes it', (_label, value) => {
		storeAttempt(value);
		expect(consumeOidcAttempt({ now: () => NOW })).toBeNull();
		expect(sessionStorage.getItem(OIDC_ATTEMPT_STORAGE_KEY)).toBeNull();
	});

	it('returns null when there is no attempt', () => {
		expect(consumeOidcAttempt()).toBeNull();
	});

	it('returns null, and still tries to delete, when storage cannot be read', () => {
		const removeItem = vi.fn();
		const storage = {
			setItem: () => {},
			removeItem,
			getItem: () => {
				throw new DOMException('denied', 'SecurityError');
			}
		};
		expect(consumeOidcAttempt({ storage })).toBeNull();
		expect(removeItem).toHaveBeenCalledWith(OIDC_ATTEMPT_STORAGE_KEY);
	});
});

describe('provider presentation', () => {
	it('uses Google’s approved wording on Google buttons', () => {
		const google = { kind: 'google' as const, display_name: 'Google' };
		expect(providerButtonLabel(google, 'login')).toBe('Sign in with Google');
		expect(providerButtonLabel(google, 'register')).toBe('Continue with Google');
	});

	it('gives every other provider a neutral label with its display name', () => {
		expect(providerButtonLabel({ kind: 'oidc', display_name: 'Test IdP' }, 'login')).toBe('Continue with Test IdP');
		expect(providerButtonLabel({ kind: 'saml', display_name: 'Corp SSO' }, 'register')).toBe('Continue with Corp SSO');
	});

	it('drops malformed provider entries and lists Google first', () => {
		const providers = signInProviders([
			{ slug: 'keycloak', display_name: 'Keycloak', kind: 'oidc' },
			{ slug: '', display_name: 'Nameless', kind: 'oidc' },
			{ display_name: 'No slug', kind: 'oidc' },
			{ slug: '..', display_name: 'Dot segment', kind: 'oidc' },
			{ slug: 'Not A Label', display_name: 'Spaces', kind: 'oidc' },
			{ slug: 'blank', display_name: '  ', kind: 'oidc' },
			null,
			{ slug: 'google', display_name: 'Google', kind: 'google' },
			{ slug: 'corp', display_name: 'Corp SSO', kind: 'saml' }
		]);
		expect(providers.map((provider) => provider.slug)).toEqual(['google', 'keycloak', 'corp']);
	});

	it('treats anything but an array as no providers', () => {
		expect(signInProviders({ detail: 'Not Found' })).toEqual([]);
		expect(signInProviders(undefined)).toEqual([]);
	});
});

describe('oidcErrorMessage', () => {
	it('has its own human-readable message for every error code the backend sends', () => {
		const messages = OIDC_ERROR_CODES.map((code) => oidcErrorMessage(code));
		const generic = oidcErrorMessage('something_new');

		for (const message of messages) {
			expect(message).toBeTruthy();
			expect(message).not.toBe(generic);
			expect(message).not.toMatch(/oidc_/);
		}
		expect(new Set(messages).size).toBe(OIDC_ERROR_CODES.length);
	});

	it('explains a cancelled sign-in as a cancellation', () => {
		expect(oidcErrorMessage('oidc_denied')).toMatch(/cancelled/i);
	});

	// The existing account may have a password, or only another provider's
	// sign-in; the message must work for both without saying which it is.
	it('sends an existing account back to either way it may sign in', () => {
		const message = oidcErrorMessage('oidc_account_exists');
		expect(message).toMatch(/username and password/);
		expect(message).toMatch(/identity provider you signed up with/);
	});

	it('falls back to a generic message for unknown codes, including Object.prototype names', () => {
		expect(oidcErrorMessage('something_new')).toMatch(/failed/i);
		expect(oidcErrorMessage('toString')).toBe(oidcErrorMessage('something_new'));
		expect(oidcErrorMessage('__proto__')).toBe(oidcErrorMessage('something_new'));
	});

	it('returns null when there is no error code', () => {
		expect(oidcErrorMessage(null)).toBeNull();
		expect(oidcErrorMessage(undefined)).toBeNull();
		expect(oidcErrorMessage('')).toBeNull();
	});
});
