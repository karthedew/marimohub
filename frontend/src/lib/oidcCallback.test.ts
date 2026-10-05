import { describe, expect, it, vi } from 'vitest';

import { ApiError, type OidcExchangeResponse } from './api';
import type { OidcAttempt } from './oidc';
import {
	completeOidcCallback,
	handoffFromHash,
	oidcCallbackMessage,
	type OidcCallbackDeps,
	type OidcCallbackFailure
} from './oidcCallback';

const VERIFIER = 'v'.repeat(43);

const SESSION: OidcExchangeResponse = {
	access_token: 'header.payload.signature',
	token_type: 'bearer',
	user: {
		id: 'u1',
		username: 'ada',
		display_name: 'Ada Lovelace',
		email: 'ada@example.com',
		created_at: '2026-01-01T00:00:00Z'
	}
};

function attempt(overrides: Partial<OidcAttempt> = {}): OidcAttempt {
	return { provider: 'google', verifier: VERIFIER, next: '/notebooks/1', created_at: Date.now(), ...overrides };
}

// Records every side effect in the order it happened.
function harness(overrides: Partial<OidcCallbackDeps> = {}) {
	const steps: string[] = [];
	const deps: OidcCallbackDeps = {
		hash: '#handoff=h-123',
		scrubUrl: vi.fn(() => void steps.push('scrub')),
		consumeAttempt: vi.fn(() => {
			steps.push('consume');
			return attempt();
		}),
		exchange: vi.fn(async () => {
			steps.push('exchange');
			return SESSION;
		}),
		setSession: vi.fn(() => void steps.push('setSession')),
		navigate: vi.fn(async () => void steps.push('navigate')),
		...overrides
	};
	return { deps, steps };
}

describe('handoffFromHash', () => {
	it.each([
		['#handoff=abc.def', 'abc.def'],
		['handoff=abc', 'abc'],
		['#state=1&handoff=abc', 'abc'],
		['#handoff=a%2Bb%2F%3D', 'a+b/='],
		// A literal `+` stays a `+`; URLSearchParams would have made it a space.
		['#handoff=a+b', 'a+b']
	])('reads the handoff from %j', (hash, expected) => {
		expect(handoffFromHash(hash)).toBe(expected);
	});

	it.each([[''], ['#'], ['#handoff='], ['#handoff'], ['#nothandoff=abc'], ['#handoff=%E0%A4%A'], [`#handoff=${'a'.repeat(4097)}`]])(
		'finds no usable handoff in %j',
		(hash) => {
			expect(handoffFromHash(hash)).toBeNull();
		}
	);
});

describe('completeOidcCallback', () => {
	it('scrubs the URL, consumes the attempt, exchanges, stores the session, then navigates — in that order', async () => {
		const { deps, steps } = harness();

		await expect(completeOidcCallback(deps)).resolves.toEqual({ ok: true, next: '/notebooks/1' });

		expect(steps).toEqual(['scrub', 'consume', 'exchange', 'setSession', 'navigate']);
		expect(deps.exchange).toHaveBeenCalledWith({ handoff: 'h-123', verifier: VERIFIER });
		expect(deps.setSession).toHaveBeenCalledWith(SESSION.access_token, SESSION.user);
		expect(deps.navigate).toHaveBeenCalledWith('/notebooks/1');
	});

	it('scrubs the URL and deletes the verifier synchronously, before any network round trip', () => {
		const { deps, steps } = harness();

		const pending = completeOidcCallback(deps);
		expect(steps).toEqual(['scrub', 'consume', 'exchange']);
		return pending;
	});

	it('never navigates to an unsafe next path from storage', async () => {
		const { deps } = harness({ consumeAttempt: () => attempt({ next: '/\\evil.example.com' }) });

		await expect(completeOidcCallback(deps)).resolves.toEqual({ ok: true, next: '/' });
		expect(deps.navigate).toHaveBeenCalledWith('/');
	});

	it('fails without contacting the backend when the URL carries no handoff, and still consumes the attempt', async () => {
		const { deps, steps } = harness({ hash: '' });

		await expect(completeOidcCallback(deps)).resolves.toEqual({ ok: false, failure: 'no_handoff', next: '/notebooks/1' });
		expect(steps).toEqual(['scrub', 'consume']);
		expect(deps.exchange).not.toHaveBeenCalled();
	});

	it('fails without contacting the backend when this tab holds no verifier (a replayed or foreign link)', async () => {
		const { deps } = harness({ consumeAttempt: () => null });

		await expect(completeOidcCallback(deps)).resolves.toEqual({ ok: false, failure: 'no_attempt', next: null });
		expect(deps.scrubUrl).toHaveBeenCalled();
		expect(deps.exchange).not.toHaveBeenCalled();
		expect(deps.setSession).not.toHaveBeenCalled();
	});

	it('reports a handoff the backend refuses, without signing in or navigating', async () => {
		const { deps } = harness({
			exchange: vi.fn().mockRejectedValue(new ApiError(401, 'Invalid or expired sign-in handoff'))
		});

		await expect(completeOidcCallback(deps)).resolves.toEqual({ ok: false, failure: 'rejected', next: '/notebooks/1' });
		expect(deps.setSession).not.toHaveBeenCalled();
		expect(deps.navigate).not.toHaveBeenCalled();
	});

	it.each([
		['a network failure', new TypeError('Failed to fetch')],
		['a server error', new ApiError(503, 'Service Unavailable')]
	])('reports %s as the backend being unavailable', async (_label, error) => {
		const { deps } = harness({ exchange: vi.fn().mockRejectedValue(error) });

		await expect(completeOidcCallback(deps)).resolves.toMatchObject({ ok: false, failure: 'unavailable' });
		expect(deps.setSession).not.toHaveBeenCalled();
	});

	it('reports a session this browser will not store, instead of leaving the page waiting', async () => {
		const { deps } = harness({
			setSession: vi.fn(() => {
				throw new DOMException('The quota has been exceeded.', 'QuotaExceededError');
			})
		});

		await expect(completeOidcCallback(deps)).resolves.toEqual({ ok: false, failure: 'storage', next: '/notebooks/1' });
		expect(deps.navigate).not.toHaveBeenCalled();
	});

	it('still completes sign-in when the URL cannot be scrubbed', async () => {
		const { deps } = harness({
			scrubUrl: () => {
				throw new DOMException('denied', 'SecurityError');
			}
		});

		await expect(completeOidcCallback(deps)).resolves.toEqual({ ok: true, next: '/notebooks/1' });
		expect(deps.setSession).toHaveBeenCalled();
	});

	it('stays signed in when the final navigation fails', async () => {
		const { deps } = harness({ navigate: vi.fn().mockRejectedValue(new Error('navigation aborted')) });

		await expect(completeOidcCallback(deps)).resolves.toEqual({ ok: true, next: '/notebooks/1' });
		expect(deps.setSession).toHaveBeenCalled();
	});
});

describe('oidcCallbackMessage', () => {
	it('has a distinct message for every failure', () => {
		const failures: OidcCallbackFailure[] = ['no_handoff', 'no_attempt', 'rejected', 'unavailable', 'storage'];
		const messages = failures.map(oidcCallbackMessage);
		expect(messages.every((message) => message.length > 0)).toBe(true);
		expect(new Set(messages).size).toBe(failures.length);
		expect(oidcCallbackMessage('no_attempt')).toMatch(/expired or has already been used/);
	});
});
