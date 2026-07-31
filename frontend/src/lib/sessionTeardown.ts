import { api } from '$lib/api';

/**
 * Best-effort session cleanup for an actual page unload (`pagehide`), as
 * opposed to in-app navigation, which awaits a normal authenticated DELETE
 * instead. `keepalive` lets the browser finish the request after the page is
 * gone; no auth header is sent because the page may already be tearing down
 * before a token lookup would matter, and holding the session id is already
 * sufficient authorization to end it. Callers must not await or chain this —
 * an unload handler that blocks on a promise can be dropped by the browser
 * before the request is even sent.
 */
export function teardownSessionOnUnload(sessionId: string): void {
	void api.sessions.delete(sessionId, { auth: false, keepalive: true });
}
