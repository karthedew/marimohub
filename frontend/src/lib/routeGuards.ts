import { redirect } from '@sveltejs/kit';
import { browser } from '$app/environment';
import { getAuthToken } from '$lib/stores/auth';

// `ssr` is off for this app specifically because bearer auth lives in
// localStorage, which the server can never see — a server-side pass would
// always look signed-out and misfire this guard. Route protection is
// therefore exclusively a client-side concern; the browser check keeps this
// safe to call from a `load` function that could in principle run during SSR.
export function requireAuth(url: URL): void {
	if (!browser) return;
	if (getAuthToken()) return;

	const next = `${url.pathname}${url.search}`;
	redirect(303, `/auth/login?next=${encodeURIComponent(next)}`);
}
