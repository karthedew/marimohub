import { execFileSync, spawn, type ChildProcess } from 'node:child_process';

import {
	backendBaseUrl,
	backendDir,
	backendPort,
	e2eOidcProvider,
	e2eOidcRedirectUri,
	frontendBaseUrl,
	frontendDir,
	frontendPort,
	idpPort,
	requireE2EDatabaseUrl
} from './env';
import { startFakeIdp } from './fakeIdp';

const secretKey = process.env.E2E_SECRET_KEY ?? 'e2e-secret-not-for-production';

async function waitForReady(url: string, timeoutMs: number, label: string) {
	const deadline = Date.now() + timeoutMs;
	let lastError: unknown;

	while (Date.now() < deadline) {
		try {
			const response = await fetch(url);
			if (response.ok) return;
			lastError = new Error(`${url} responded ${response.status}`);
		} catch (error) {
			lastError = error;
		}
		await new Promise((resolve) => setTimeout(resolve, 300));
	}

	throw new Error(`${label} did not become ready within ${timeoutMs}ms: ${String(lastError)}`);
}

function killTree(child: ChildProcess) {
	if (child.exitCode !== null || child.killed) return;
	child.kill('SIGTERM');
}

// The backend loads its sentence-embedding model on first use: the first
// publish out of Private, which then takes about 5 s (far longer if the model
// must be downloaded first). Publishing one notebook here keeps that out of
// whichever spec publishes first, where it would race a 5 s assertion.
async function warmUpBackend() {
	async function post<T>(path: string, body: unknown, token?: string): Promise<T> {
		const response = await fetch(`${backendBaseUrl}${path}`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
			body: JSON.stringify(body)
		});
		if (!response.ok) {
			throw new Error(`warming up the backend: POST ${path} responded ${response.status}: ${await response.text()}`);
		}
		return (await response.json()) as T;
	}

	// The database is rebuilt on every run, so fixed names never collide.
	const username = 'e2ewarmup';
	const password = 'password123';
	await post('/api/auth/register', { username, email: `${username}@example.com`, password });
	const { access_token: token } = await post<{ access_token: string }>('/api/auth/login', { username, password });
	const workspace = await post<{ id: string }>('/api/workspaces', { name: 'Warm-up Space' }, token);
	const notebook = await post<{ id: string }>('/api/notebooks', { title: 'Warm-up Notebook', workspace_id: workspace.id }, token);
	await post(`/api/notebooks/${notebook.id}/publish`, { visibility: 'public' }, token);
}

/**
 * Brings up an isolated fake identity provider + backend + built frontend for
 * browser tests, and returns a teardown function Playwright calls once the
 * run finishes.
 *
 * This intentionally never touches the development database (`molab`, from
 * `make dev-db`) or a running dev server: it uses its own ports and its own
 * throwaway database, so a developer can run both alongside this suite.
 */
export default async function globalSetup() {
	const databaseUrl = requireE2EDatabaseUrl();

	const alembicEnv = { ...process.env, DATABASE_URL: databaseUrl };
	execFileSync('uv', ['run', 'alembic', 'downgrade', 'base'], {
		cwd: backendDir,
		env: alembicEnv,
		stdio: 'inherit'
	});
	execFileSync('uv', ['run', 'alembic', 'upgrade', 'head'], {
		cwd: backendDir,
		env: alembicEnv,
		stdio: 'inherit'
	});

	// Run in reverse on teardown, or as soon as any step below fails.
	const stops: Array<() => void | Promise<void>> = [];
	const teardown = async () => {
		for (const stop of stops.splice(0).reverse()) {
			try {
				await stop();
			} catch (error) {
				console.error('e2e teardown step failed:', error);
			}
		}
	};

	try {
		// Up before the backend, which fetches its discovery document on the
		// first provider sign-in.
		const idp = await startFakeIdp({
			port: Number(idpPort),
			issuer: e2eOidcProvider.issuer,
			clientId: e2eOidcProvider.client_id,
			clientSecret: e2eOidcProvider.client_secret,
			redirectUris: [e2eOidcRedirectUri]
		});
		stops.push(() => idp.close());

		const backend = spawn(
			'uv',
			['run', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', backendPort],
			{
				cwd: backendDir,
				env: {
					...process.env,
					DATABASE_URL: databaseUrl,
					SECRET_KEY: secretKey,
					SESSION_BACKEND: 'subprocess',
					PUBLIC_API_URL: backendBaseUrl,
					// Provider sign-in lands back on this suite's frontend, which is
					// also the extra origin the backend allows for CORS.
					PUBLIC_APP_URL: frontendBaseUrl,
					OIDC_PROVIDERS: JSON.stringify([e2eOidcProvider]),
					// Blank means unset to the backend. These override anything a
					// developer keeps in `backend/.env`, which the backend also reads,
					// so the only provider is the fake one and nothing is proxied.
					GOOGLE_CLIENT_ID: '',
					GOOGLE_CLIENT_SECRET: '',
					GOOGLE_HOSTED_DOMAIN: '',
					OIDC_HTTP_PROXY_URL: ''
				},
				stdio: 'inherit'
			}
		);
		stops.push(() => killTree(backend));
		await waitForReady(`${backendBaseUrl}/api/health`, 30_000, 'backend');
		await warmUpBackend();

		execFileSync('npm', ['run', 'build'], {
			cwd: frontendDir,
			env: { ...process.env, PUBLIC_API_URL: backendBaseUrl },
			stdio: 'inherit'
		});

		const frontend = spawn('node', ['build'], {
			cwd: frontendDir,
			env: {
				...process.env,
				HOST: '127.0.0.1',
				PORT: frontendPort,
				ORIGIN: frontendBaseUrl,
				PUBLIC_API_URL: backendBaseUrl
			},
			stdio: 'inherit'
		});
		stops.push(() => killTree(frontend));
		await waitForReady(frontendBaseUrl, 30_000, 'frontend');
	} catch (error) {
		await teardown();
		throw error;
	}

	return teardown;
}
