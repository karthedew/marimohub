import { json } from '@sveltejs/kit';

// Liveness/readiness probe target. Deliberately outside `/api/*`, which is
// reserved for the backend the frontend proxies to, and independent of the
// root layout's `ssr = false` (server endpoints always run on the server
// regardless of how pages above them render).
export function GET() {
	return json({ status: 'ok' });
}
