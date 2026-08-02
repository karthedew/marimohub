// Production entrypoint for the built SvelteKit app. This replaces the
// generated `build/index.js` (what `node build` runs) rather than wrapping
// it, for the same reason the backend has its own `app.serve` instead of
// invoking uvicorn's CLI directly: neither an HTTPS listener nor
// certificate-rotation-without-a-restart is something the generated
// handler owns, and adapter-node's own docs document exactly this "import
// the handler, build your own server" pattern for exactly this situation.
//
// `handler` is a plain `(req, res, next) => void` Node request listener
// with no dependency of its own on how the socket was opened, so passing it
// to `https.createServer` instead of `http.createServer` is the entire
// integration.
import fs from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import process from 'node:process';

import { handler } from './build/handler.js';

const host = process.env.HOST ?? '0.0.0.0';
const port = Number(process.env.PORT ?? 3000);
const certFile = process.env.TLS_CERT_FILE;
const keyFile = process.env.TLS_KEY_FILE;
const pollIntervalMs = Number(process.env.TLS_CERT_POLL_INTERVAL_SECONDS ?? 5) * 1000;
const gracefulTimeoutMs = Number(process.env.GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS ?? 30) * 1000;
const useTls = Boolean(certFile && keyFile);

function readTlsOptions() {
	return { cert: fs.readFileSync(certFile), key: fs.readFileSync(keyFile) };
}

// Kubernetes projects a Secret volume as a symlink into a versioned
// `..data_<timestamp>` directory and swaps that symlink atomically on
// rotation; an `fs.watch()` on the original path does not survive that
// swap, so this polls `fs.statSync` on the resolved path instead of relying
// on filesystem change notifications that do not cross it.
function statFingerprint(path) {
	try {
		const stats = fs.statSync(path);
		return `${stats.mtimeNs}:${stats.ino}`;
	} catch {
		return null;
	}
}

const server = useTls ? https.createServer(readTlsOptions(), handler) : http.createServer(handler);

if (useTls) {
	let last = `${statFingerprint(certFile)}:${statFingerprint(keyFile)}`;
	const watch = setInterval(() => {
		const current = `${statFingerprint(certFile)}:${statFingerprint(keyFile)}`;
		if (current === last) return;
		try {
			// Updates the certificate used for future TLS handshakes only.
			// Connections that already completed their handshake keep using
			// the context they negotiated with; nothing about them changes.
			server.setSecureContext(readTlsOptions());
			last = current;
			console.log(`reloaded rotated certificate from ${certFile}`);
		} catch (error) {
			console.error(`certificate rotation failed to load ${certFile}; keeping prior certificate`, error);
		}
	}, pollIntervalMs);
	watch.unref();
}

server.listen(port, host, () => {
	console.log(`listening on ${useTls ? 'https' : 'http'}://${host}:${port}`);
});

let shuttingDown = false;

function shutdown(signal) {
	if (shuttingDown) return;
	shuttingDown = true;
	console.log(`received ${signal}; draining in-flight connections`);

	// A connection sitting idle on keep-alive would otherwise block close()
	// forever; closing those immediately still lets any connection with a
	// request actually in flight finish normally.
	server.closeIdleConnections();
	server.close(() => {
		console.log('all connections drained, exiting');
		process.exit(0);
	});

	setTimeout(() => {
		console.warn(`graceful shutdown timeout (${gracefulTimeoutMs}ms) exceeded; forcing remaining connections closed`);
		server.closeAllConnections();
	}, gracefulTimeoutMs).unref();
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
