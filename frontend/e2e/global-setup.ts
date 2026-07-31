import { execFileSync, spawn, type ChildProcess } from 'node:child_process';

import {
	backendBaseUrl,
	backendDir,
	backendPort,
	frontendBaseUrl,
	frontendDir,
	frontendPort,
	requireE2EDatabaseUrl
} from './env';

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

/**
 * Brings up an isolated backend + built frontend pair for browser tests, and
 * returns a teardown function Playwright calls once the run finishes.
 *
 * This intentionally never touches `podman-compose.yml`'s dev stack: it uses
 * its own ports and its own throwaway database so a developer can run the
 * dev stack and this suite at the same time without collisions.
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
				PUBLIC_API_URL: backendBaseUrl
			},
			stdio: 'inherit'
		}
	);

	try {
		await waitForReady(`${backendBaseUrl}/api/health`, 30_000, 'backend');
	} catch (error) {
		killTree(backend);
		throw error;
	}

	let frontend: ChildProcess;
	try {
		execFileSync('npm', ['run', 'build'], {
			cwd: frontendDir,
			env: { ...process.env, PUBLIC_API_URL: backendBaseUrl },
			stdio: 'inherit'
		});

		frontend = spawn('node', ['build'], {
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

		await waitForReady(frontendBaseUrl, 30_000, 'frontend');
	} catch (error) {
		killTree(backend);
		throw error;
	}

	return async () => {
		killTree(backend);
		killTree(frontend);
	};
}
