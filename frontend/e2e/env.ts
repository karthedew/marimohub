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
// The backend allows cross-origin calls from `http://localhost:5173` (the Vite
// dev-server default) plus the origin of `PUBLIC_APP_URL`, which global setup
// sets to this URL — so E2E_FRONTEND_PORT can move the frontend off 5173, e.g.
// while `npm run dev` holds it. The same URL is where the backend sends the
// browser after a provider sign-in (`/auth/callback`, or `/auth/login?error=`).
export const frontendBaseUrl = `http://localhost:${frontendPort}`;

// The fake OpenID provider from `fakeIdp.ts`, started by global setup.
export const idpPort = process.env.E2E_IDP_PORT ?? '8111';
// Test code talks to its control endpoint directly on loopback; only the OIDC
// flow itself (backend and browser) goes through the issuer URL below.
export const idpControlUrl = `http://127.0.0.1:${idpPort}`;

// The provider the e2e backend is configured with, in `OIDC_PROVIDERS` shape.
// The issuer has no trailing slash and must match the fake provider's
// discovery document and `iss` claims byte-for-byte.
export const e2eOidcProvider = {
	kind: 'oidc',
	slug: 'e2e',
	display_name: 'Test IdP',
	issuer: `http://localhost:${idpPort}`,
	client_id: 'e2e-client',
	client_secret: 'e2e-secret'
} as const;

// The only redirect URI the fake provider accepts: the backend derives it
// from PUBLIC_API_URL, so any drift in that derivation fails sign-in loudly.
export const e2eOidcRedirectUri = `${backendBaseUrl}/api/auth/oidc/${e2eOidcProvider.slug}/callback`;

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
