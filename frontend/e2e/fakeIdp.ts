// A minimal OpenID Connect provider for the browser tests, built on node:http
// and node:crypto. It implements just enough of the authorization-code flow
// (discovery, authorize, token, JWKS) for the backend's real OIDC client to run
// end to end, and makes the checks a real provider makes — registered client
// and redirect URI, client credentials, single-use codes, PKCE S256 — so a
// backend regression fails here rather than only against Google.
//
// /authorize never shows a page. It approves at once, as the identity a test
// queued through the control endpoint (or a default one), or it denies: when a
// test queued `{ deny: true }`, or the request carries `login_hint=deny`.
//
// Kept free of local imports so it also runs directly under Node.
import { createHash, generateKeyPairSync, randomBytes, sign, timingSafeEqual, type KeyObject } from 'node:crypto';
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';

export type FakeIdentity = {
	sub: string;
	email: string;
	email_verified?: boolean;
	name?: string;
	preferred_username?: string;
};

export type ScriptedLogin = FakeIdentity | { deny: true };

export type FakeIdpOptions = {
	port: number;
	// Exactly as the backend is configured: no trailing slash, no path.
	issuer: string;
	clientId: string;
	clientSecret: string;
	redirectUris: readonly string[];
	host?: string;
};

export type FakeIdp = {
	issuer: string;
	close: () => Promise<void>;
};

type IssuedCode = {
	redirectUri: string;
	nonce: string;
	codeChallenge: string;
	scope: string;
	identity: FakeIdentity;
	authTime: number;
	expiresAt: number;
};

const CONTROL_PATH = '/__e2e/next-login';
const RESET_PATH = '/__e2e/reset';
const CODE_TTL_MS = 60_000;
const ID_TOKEN_TTL_SECONDS = 300;
const MAX_BODY_BYTES = 64 * 1024;
const CHALLENGE_PATTERN = /^[A-Za-z0-9_-]{43}$/;
const VERIFIER_PATTERN = /^[A-Za-z0-9\-._~]{43,128}$/;

const DEFAULT_IDENTITY: FakeIdentity = {
	sub: 'e2e-default-user',
	email: 'e2e-default-user@example.com',
	email_verified: true,
	name: 'E2E Default User',
	preferred_username: 'e2e-default-user'
};

class HttpError extends Error {
	readonly status: number;

	constructor(status: number, message: string) {
		super(message);
		this.status = status;
	}
}

function base64url(value: string | Buffer) {
	return Buffer.from(value).toString('base64url');
}

function sameSecret(actual: string, expected: string) {
	const a = Buffer.from(actual);
	const b = Buffer.from(expected);
	return a.length === b.length && timingSafeEqual(a, b);
}

function signJwt(claims: Record<string, unknown>, key: KeyObject, kid: string) {
	const signingInput = `${base64url(JSON.stringify({ alg: 'RS256', typ: 'JWT', kid }))}.${base64url(JSON.stringify(claims))}`;
	return `${signingInput}.${sign('sha256', Buffer.from(signingInput), key).toString('base64url')}`;
}

function send(res: ServerResponse, status: number, body: unknown, headers: Record<string, string> = {}) {
	const payload = typeof body === 'string' ? body : JSON.stringify(body);
	res.writeHead(status, {
		'Content-Type': typeof body === 'string' ? 'text/plain; charset=utf-8' : 'application/json',
		'Cache-Control': 'no-store',
		...headers
	});
	res.end(payload);
}

function redirect(res: ServerResponse, location: string) {
	res.writeHead(302, { Location: location, 'Cache-Control': 'no-store' });
	res.end();
}

// RFC 6749 §5.2 error response from the token endpoint.
function tokenError(res: ServerResponse, status: number, error: string, description: string, headers: Record<string, string> = {}) {
	send(res, status, { error, error_description: description }, { Pragma: 'no-cache', ...headers });
}

async function readBody(req: IncomingMessage) {
	const chunks: Buffer[] = [];
	let size = 0;
	for await (const chunk of req) {
		size += (chunk as Buffer).length;
		if (size > MAX_BODY_BYTES) throw new HttpError(413, 'request body too large');
		chunks.push(chunk as Buffer);
	}
	return Buffer.concat(chunks).toString('utf8');
}

// RFC 6749 §2.3.1: both halves are form-urlencoded before base64.
function parseBasicCredentials(header: string) {
	const match = /^Basic\s+([A-Za-z0-9+/=]+)\s*$/i.exec(header);
	if (!match) return null;
	const decoded = Buffer.from(match[1], 'base64').toString('utf8');
	const separator = decoded.indexOf(':');
	if (separator < 0) return null;
	const formDecode = (value: string) => decodeURIComponent(value.replace(/\+/g, ' '));
	try {
		return { clientId: formDecode(decoded.slice(0, separator)), clientSecret: formDecode(decoded.slice(separator + 1)) };
	} catch {
		return null;
	}
}

function parseScriptedLogin(value: unknown): ScriptedLogin {
	if (!value || typeof value !== 'object') throw new HttpError(400, 'expected a JSON object');
	const fields = value as Record<string, unknown>;
	if (fields.deny === true) return { deny: true };
	if (typeof fields.sub !== 'string' || !fields.sub || typeof fields.email !== 'string' || !fields.email) {
		throw new HttpError(400, 'a scripted identity needs non-empty sub and email strings');
	}
	const identity: FakeIdentity = {
		sub: fields.sub,
		email: fields.email,
		email_verified: fields.email_verified === undefined ? true : fields.email_verified === true
	};
	if (typeof fields.name === 'string') identity.name = fields.name;
	if (typeof fields.preferred_username === 'string') identity.preferred_username = fields.preferred_username;
	return identity;
}

export async function startFakeIdp(options: FakeIdpOptions): Promise<FakeIdp> {
	const issuer = options.issuer;
	if (new URL(issuer).pathname !== '/' || issuer.endsWith('/')) {
		throw new Error(`fake IdP issuer must be an origin with no trailing slash, got ${issuer}`);
	}

	const { privateKey, publicKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
	const kid = randomBytes(8).toString('hex');
	const jwks = { keys: [{ ...publicKey.export({ format: 'jwk' }), kid, alg: 'RS256', use: 'sig' }] };
	const discovery = {
		issuer,
		authorization_endpoint: `${issuer}/authorize`,
		token_endpoint: `${issuer}/token`,
		jwks_uri: `${issuer}/jwks`,
		response_types_supported: ['code'],
		response_modes_supported: ['query'],
		grant_types_supported: ['authorization_code'],
		subject_types_supported: ['public'],
		id_token_signing_alg_values_supported: ['RS256'],
		scopes_supported: ['openid', 'email', 'profile'],
		token_endpoint_auth_methods_supported: ['client_secret_basic', 'client_secret_post'],
		code_challenge_methods_supported: ['S256'],
		claims_supported: ['iss', 'sub', 'aud', 'exp', 'iat', 'auth_time', 'nonce', 'email', 'email_verified', 'name', 'preferred_username'],
		authorization_response_iss_parameter_supported: true
	};

	const scripted: ScriptedLogin[] = [];
	const codes = new Map<string, IssuedCode>();

	function authorize(url: URL, res: ServerResponse) {
		const params = url.searchParams;
		// Until the client and its redirect URI check out, errors go to the
		// browser, never to an unverified redirect target (RFC 6749 §4.1.2.1).
		if (params.get('client_id') !== options.clientId) return send(res, 400, 'unknown client_id');
		const redirectUri = params.get('redirect_uri') ?? '';
		if (!options.redirectUris.includes(redirectUri)) return send(res, 400, `unregistered redirect_uri: ${redirectUri}`);

		const respond = (fields: Record<string, string>) => {
			const target = new URL(redirectUri);
			for (const [key, value] of Object.entries(fields)) target.searchParams.set(key, value);
			const state = params.get('state');
			if (state !== null) target.searchParams.set('state', state);
			target.searchParams.set('iss', issuer); // RFC 9207
			redirect(res, target.toString());
		};
		const fail = (error: string, description: string) => respond({ error, error_description: description });

		if (params.get('response_type') !== 'code') return fail('unsupported_response_type', 'only response_type=code is supported');
		const scope = params.get('scope') ?? '';
		if (!scope.split(' ').includes('openid')) return fail('invalid_scope', 'scope must include openid');
		if (!params.get('state')) return fail('invalid_request', 'this provider requires state');
		const nonce = params.get('nonce');
		if (!nonce) return fail('invalid_request', 'this provider requires nonce');
		const codeChallenge = params.get('code_challenge') ?? '';
		if (params.get('code_challenge_method') !== 'S256' || !CHALLENGE_PATTERN.test(codeChallenge)) {
			return fail('invalid_request', 'this provider requires PKCE with code_challenge_method=S256');
		}

		const outcome: ScriptedLogin = params.get('login_hint') === 'deny' ? { deny: true } : (scripted.shift() ?? DEFAULT_IDENTITY);
		if ('deny' in outcome) return fail('access_denied', 'The user denied the request');

		const code = randomBytes(32).toString('base64url');
		codes.set(code, {
			redirectUri,
			nonce,
			codeChallenge,
			scope,
			identity: outcome,
			authTime: Math.floor(Date.now() / 1000),
			expiresAt: Date.now() + CODE_TTL_MS
		});
		respond({ code });
	}

	async function token(req: IncomingMessage, res: ServerResponse) {
		if (!(req.headers['content-type'] ?? '').toLowerCase().startsWith('application/x-www-form-urlencoded')) {
			return tokenError(res, 400, 'invalid_request', 'expected an application/x-www-form-urlencoded body');
		}
		const body = new URLSearchParams(await readBody(req));

		// Client authentication: client_secret_basic or client_secret_post, never both.
		const authorization = req.headers.authorization;
		const postedSecret = body.get('client_secret');
		if (authorization !== undefined) {
			const basic = parseBasicCredentials(authorization);
			const challenge = { 'WWW-Authenticate': 'Basic realm="fake-idp"' };
			if (!basic) return tokenError(res, 401, 'invalid_client', 'unsupported Authorization header', challenge);
			if (postedSecret !== null) return tokenError(res, 400, 'invalid_request', 'use one client authentication method');
			const postedId = body.get('client_id');
			if (postedId !== null && postedId !== basic.clientId) {
				return tokenError(res, 400, 'invalid_request', 'client_id does not match the Authorization header');
			}
			if (basic.clientId !== options.clientId || !sameSecret(basic.clientSecret, options.clientSecret)) {
				return tokenError(res, 401, 'invalid_client', 'bad client credentials', challenge);
			}
		} else if (postedSecret !== null) {
			if (body.get('client_id') !== options.clientId || !sameSecret(postedSecret, options.clientSecret)) {
				return tokenError(res, 401, 'invalid_client', 'bad client credentials');
			}
		} else {
			return tokenError(res, 401, 'invalid_client', 'client authentication is required');
		}

		if (body.get('grant_type') !== 'authorization_code') {
			return tokenError(res, 400, 'unsupported_grant_type', 'only authorization_code is supported');
		}
		const code = body.get('code') ?? '';
		const issued = codes.get(code);
		codes.delete(code); // single use, even when the rest of this request fails
		if (!issued || issued.expiresAt < Date.now()) {
			return tokenError(res, 400, 'invalid_grant', 'unknown, expired, or already used code');
		}
		if (body.get('redirect_uri') !== issued.redirectUri) {
			return tokenError(res, 400, 'invalid_grant', 'redirect_uri does not match the authorization request');
		}
		const verifier = body.get('code_verifier') ?? '';
		if (!VERIFIER_PATTERN.test(verifier)) return tokenError(res, 400, 'invalid_grant', 'a valid code_verifier is required');
		if (createHash('sha256').update(verifier).digest('base64url') !== issued.codeChallenge) {
			return tokenError(res, 400, 'invalid_grant', 'code_verifier does not match code_challenge');
		}

		const now = Math.floor(Date.now() / 1000);
		const { identity } = issued;
		const idToken = signJwt(
			{
				iss: issuer,
				sub: identity.sub,
				aud: options.clientId,
				iat: now,
				exp: now + ID_TOKEN_TTL_SECONDS,
				auth_time: issued.authTime,
				nonce: issued.nonce,
				email: identity.email,
				email_verified: identity.email_verified ?? true,
				...(identity.name === undefined ? {} : { name: identity.name }),
				...(identity.preferred_username === undefined ? {} : { preferred_username: identity.preferred_username })
			},
			privateKey,
			kid
		);
		send(
			res,
			200,
			{
				access_token: randomBytes(32).toString('base64url'),
				token_type: 'Bearer',
				expires_in: ID_TOKEN_TTL_SECONDS,
				scope: issued.scope,
				id_token: idToken
			},
			{ Pragma: 'no-cache' }
		);
	}

	async function control(req: IncomingMessage, res: ServerResponse) {
		let value: unknown;
		try {
			value = JSON.parse(await readBody(req));
		} catch (error) {
			if (error instanceof HttpError) throw error;
			throw new HttpError(400, 'expected a JSON body');
		}
		scripted.push(parseScriptedLogin(value));
		res.writeHead(204);
		res.end();
	}

	async function handle(req: IncomingMessage, res: ServerResponse) {
		// Concatenated rather than resolved, so a request for `//path` stays a
		// path instead of being read as another host.
		const url = new URL(`http://fake-idp.invalid${req.url ?? '/'}`);
		const route = `${req.method} ${url.pathname}`;

		if (route === 'GET /.well-known/openid-configuration') return send(res, 200, discovery);
		if (route === 'GET /jwks') return send(res, 200, jwks, { 'Cache-Control': 'public, max-age=300' });
		if (route === 'GET /authorize') return authorize(url, res);
		if (route === 'POST /token') return token(req, res);
		if (route === `POST ${CONTROL_PATH}`) return control(req, res);
		if (route === `POST ${RESET_PATH}`) {
			scripted.length = 0;
			res.writeHead(204);
			return res.end();
		}
		send(res, 404, { error: 'not_found', path: url.pathname });
	}

	const server = createServer((req, res) => {
		handle(req, res).catch((error: unknown) => {
			const status = error instanceof HttpError ? error.status : 500;
			if (status === 500) console.error('[fake-idp]', error);
			if (!res.headersSent) send(res, status, { error: status === 500 ? 'server_error' : 'invalid_request', error_description: String(error) });
			else res.end();
		});
	});

	await new Promise<void>((resolve, reject) => {
		server.once('error', (error: NodeJS.ErrnoException) => {
			reject(
				error.code === 'EADDRINUSE'
					? new Error(`fake IdP: port ${options.port} is in use; set E2E_IDP_PORT to a free port`)
					: error
			);
		});
		server.listen(options.port, options.host ?? '127.0.0.1', () => resolve());
	});

	return {
		issuer,
		close: () =>
			new Promise<void>((resolve, reject) => {
				server.close((error) => (error ? reject(error) : resolve()));
				server.closeAllConnections();
			})
	};
}

// --- Test-side control client: queue what the next /authorize request does. ---

async function postControl(controlUrl: string, path: string, body?: unknown) {
	const response = await fetch(`${controlUrl}${path}`, {
		method: 'POST',
		headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
		body: body === undefined ? undefined : JSON.stringify(body)
	});
	if (!response.ok) throw new Error(`fake IdP ${path} answered ${response.status}: ${await response.text()}`);
}

// The next sign-in at the fake provider approves as `identity`, or denies.
export function scriptFakeIdpLogin(controlUrl: string, outcome: ScriptedLogin) {
	return postControl(controlUrl, CONTROL_PATH, outcome);
}

// Drops anything queued by an earlier test that never reached /authorize.
export function resetFakeIdp(controlUrl: string) {
	return postControl(controlUrl, RESET_PATH);
}
