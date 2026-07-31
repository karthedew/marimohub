import path from 'node:path';
import { fileURLToPath } from 'node:url';

const frontendDir = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

export const repoRoot = path.dirname(frontendDir);
export const backendDir = path.join(repoRoot, 'backend');
export { frontendDir };

export const backendPort = process.env.E2E_BACKEND_PORT ?? '8100';
export const frontendPort = process.env.E2E_FRONTEND_PORT ?? '5173';
// `localhost`, not `127.0.0.1`: the marimo session iframe is embedded
// cross-origin from the frontend, and marimo's own auth redirect sets a
// SameSite=Lax cookie. A browser only attaches a Lax cookie to a subframe's
// own requests when the subframe shares a *site* with the top document —
// `localhost` and `127.0.0.1` are different sites even though they're the
// same host, which strands the iframe in an endless auth redirect. Matching
// the frontend's `localhost` origin keeps the session frame same-site.
export const backendBaseUrl = `http://localhost:${backendPort}`;
// The backend's CORS policy allows exactly one browser origin
// (`http://localhost:5173`, the Vite dev-server default) and that is backend
// configuration this suite cannot change. The frontend origin below has to
// match it byte-for-byte — scheme, host, and port — or every cross-origin
// fetch the app makes fails preflight before this suite gets to test anything.
export const frontendBaseUrl = `http://localhost:${frontendPort}`;

/**
 * The dedicated E2E database name. Every setup/teardown step that can drop or
 * rewrite data must go through this guard first: `E2E_DATABASE_URL` is the
 * only database this suite is ever allowed to run destructive migrations
 * against, so a misconfigured environment variable can never point a
 * downgrade/upgrade cycle at a developer's real database.
 */
export function requireE2EDatabaseUrl(): string {
	const databaseUrl = process.env.E2E_DATABASE_URL ?? '';
	if (!databaseUrl.includes('/molab_e2e')) {
		throw new Error('E2E_DATABASE_URL must target molab_e2e');
	}
	return databaseUrl;
}
