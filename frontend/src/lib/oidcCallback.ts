// Finishing a provider sign-in on `/auth/callback`. The backend's callback
// lands the browser here with `#handoff=<token>`; see `$lib/oidc` for why
// that token is only redeemable together with this tab's stored verifier.
//
// Everything with a side effect is passed in, so the page stays a thin shell
// and the order of steps below is covered by unit tests.
import { ApiError, type OidcExchangeRequest, type OidcExchangeResponse, type User } from './api';
import type { OidcAttempt } from './oidc';
import { safeNextPath } from './safeNextPath';

const MAX_HANDOFF_LENGTH = 4096;

export type OidcCallbackFailure =
	// The URL carried no handoff: nothing to finish.
	| 'no_handoff'
	// This tab holds no (fresh) verifier: the link was already used, expired,
	// or was started in a different tab or browser.
	| 'no_attempt'
	// The backend refused the handoff: expired, or not bound to this verifier.
	| 'rejected'
	// The exchange did not get an answer (network or server error).
	| 'unavailable'
	// The browser would not store the session (site storage full or blocked).
	| 'storage';

export type OidcCallbackResult =
	| { ok: true; next: string }
	| { ok: false; failure: OidcCallbackFailure; next: string | null };

export type OidcCallbackDeps = {
	hash: string;
	scrubUrl: () => void;
	consumeAttempt: () => OidcAttempt | null;
	exchange: (body: OidcExchangeRequest) => Promise<OidcExchangeResponse>;
	setSession: (token: string, user: User) => void;
	navigate: (path: string) => unknown;
};

// The handoff from a fragment like `#handoff=abc`. Decoded with
// `decodeURIComponent` rather than URLSearchParams, which would turn a
// literal `+` into a space.
export function handoffFromHash(hash: string): string | null {
	const fragment = hash.startsWith('#') ? hash.slice(1) : hash;
	for (const part of fragment.split('&')) {
		const separator = part.indexOf('=');
		if (separator < 0 || part.slice(0, separator) !== 'handoff') continue;
		try {
			const value = decodeURIComponent(part.slice(separator + 1));
			return value.length > 0 && value.length <= MAX_HANDOFF_LENGTH ? value : null;
		} catch {
			return null;
		}
	}
	return null;
}

export async function completeOidcCallback(deps: OidcCallbackDeps): Promise<OidcCallbackResult> {
	// Everything up to the first `await` runs synchronously when the page calls
	// this, so the handoff leaves the address bar and the history entry before
	// anything else gets a chance to run, and the verifier is deleted whatever
	// happens next.
	const handoff = handoffFromHash(deps.hash);
	try {
		deps.scrubUrl();
	} catch {
		// Still safe to continue: without this tab's verifier the handoff is useless.
	}
	const attempt = deps.consumeAttempt();

	if (!handoff) return { ok: false, failure: 'no_handoff', next: attempt ? safeNextPath(attempt.next) : null };
	if (!attempt) return { ok: false, failure: 'no_attempt', next: null };

	const next = safeNextPath(attempt.next);
	let session: OidcExchangeResponse;
	try {
		session = await deps.exchange({ handoff, verifier: attempt.verifier });
	} catch (error) {
		const rejected = error instanceof ApiError && error.status >= 400 && error.status < 500;
		return { ok: false, failure: rejected ? 'rejected' : 'unavailable', next };
	}

	try {
		deps.setSession(session.access_token, session.user);
	} catch {
		return { ok: false, failure: 'storage', next };
	}
	try {
		await deps.navigate(next);
	} catch {
		// Signed in either way; the page offers a link to `next` if it is still showing.
	}
	return { ok: true, next };
}

const FAILURE_MESSAGES: Record<OidcCallbackFailure, string> = {
	no_handoff: 'There is no sign-in to finish here. Start again from the sign-in page.',
	no_attempt:
		'This sign-in link has expired or has already been used. Sign-in links work once, in the browser tab that started signing in.',
	rejected: 'This sign-in link is no longer valid for this browser tab. Please sign in again.',
	unavailable: 'MarimoHub could not finish signing you in. Check your connection and try again.',
	storage:
		'This browser would not save your sign-in, usually because storage for this site is full or blocked. Free up or allow storage for this site, then sign in again.'
};

export function oidcCallbackMessage(failure: OidcCallbackFailure) {
	return FAILURE_MESSAGES[failure];
}
