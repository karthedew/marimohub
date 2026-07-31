import { describe, expect, it } from 'vitest';

import { fakeJwt } from '../test/fakeJwt';
import { api, apiWithFetch, normalizeTagInput, readError } from './api';
import { auth, getAuthToken } from './stores/auth';

describe('normalizeTagInput', () => {
	it('splits comma-separated input and trims whitespace', () => {
		expect(normalizeTagInput('ml, data-science ,  viz')).toEqual(['ml', 'data-science', 'viz']);
	});

	it('drops empty segments produced by trailing separators', () => {
		expect(normalizeTagInput('ml,,  ,viz,')).toEqual(['ml', 'viz']);
	});

	it('passes an existing array through the same trim/filter rules', () => {
		expect(normalizeTagInput([' ml ', '', 'viz'])).toEqual(['ml', 'viz']);
	});

	it('returns an empty array for undefined input', () => {
		expect(normalizeTagInput(undefined)).toEqual([]);
	});
});

describe('readError', () => {
	it('returns a string detail as-is', async () => {
		const response = new Response(JSON.stringify({ detail: 'Workspace slug already in use' }), { status: 409 });
		await expect(readError(response)).resolves.toBe('Workspace slug already in use');
	});

	it('joins a FastAPI validation array detail into one message', async () => {
		const response = new Response(
			JSON.stringify({ detail: [{ msg: 'field required' }, { msg: 'too short' }] }),
			{ status: 422 }
		);
		await expect(readError(response)).resolves.toBe('field required, too short');
	});

	it('falls back to statusText when the body is not JSON', async () => {
		const response = new Response('not json', { status: 500, statusText: 'Server Error' });
		await expect(readError(response)).resolves.toBe('Server Error');
	});
});

type MockAnswer = { status: number; body?: unknown; statusText?: string };

function jsonAnswer(status: number, body: unknown = {}): MockAnswer {
	return { status, body };
}

function mockFetcher(answers: MockAnswer | MockAnswer[]) {
	const queue = Array.isArray(answers) ? [...answers] : [answers];
	const calls: Array<{ url: string; init: RequestInit }> = [];
	const fetcher = (async (url: string | URL, init: RequestInit = {}) => {
		calls.push({ url: String(url), init });
		const answer = queue.length > 1 ? queue.shift()! : queue[0];
		if (answer.status === 204) return new Response(null, { status: 204 });
		return new Response(JSON.stringify(answer.body ?? {}), {
			status: answer.status,
			statusText: answer.statusText,
			headers: { 'Content-Type': 'application/json' }
		});
	}) as unknown as typeof fetch;
	return { fetcher, calls };
}

describe('query serialization', () => {
	it('serializes list filters as repeated parameters, never a joined string', async () => {
		const { fetcher, calls } = mockFetcher(jsonAnswer(200, { items: [], total: 0, page: 1, page_size: 20 }));
		await apiWithFetch(fetcher).notebooks.list({ tags: ['ml', 'viz'], q: 'signals', workspace_id: 'w1' });

		const url = new URL(calls[0].url, 'http://localhost');
		expect(url.searchParams.getAll('tags')).toEqual(['ml', 'viz']);
		expect(url.searchParams.get('q')).toBe('signals');
		expect(url.searchParams.get('workspace_id')).toBe('w1');
	});

	it('omits the tags parameter entirely when there are none', async () => {
		const { fetcher, calls } = mockFetcher(jsonAnswer(200, { items: [], total: 0, page: 1, page_size: 20 }));
		await apiWithFetch(fetcher).notebooks.list({});

		const url = new URL(calls[0].url, 'http://localhost');
		expect(url.searchParams.has('tags')).toBe(false);
	});
});

describe('request bodies match the frozen backend contract', () => {
	it('sends register and login with no Authorization header', async () => {
		const { fetcher: registerFetch, calls: registerCalls } = mockFetcher(
			jsonAnswer(201, { id: 'u1', username: 'ada', email: 'ada@example.com', created_at: '2026-01-01T00:00:00Z' })
		);
		await apiWithFetch(registerFetch).auth.register({ username: 'ada', email: 'ada@example.com', password: 'password123' });
		expect(JSON.parse(String(registerCalls[0].init.body))).toEqual({
			username: 'ada',
			email: 'ada@example.com',
			password: 'password123'
		});
		expect(new Headers(registerCalls[0].init.headers).has('Authorization')).toBe(false);

		const { fetcher: loginFetch, calls: loginCalls } = mockFetcher(
			jsonAnswer(200, { access_token: 'tok', token_type: 'bearer' })
		);
		await apiWithFetch(loginFetch).auth.login({ username: 'ada', password: 'password123' });
		expect(JSON.parse(String(loginCalls[0].init.body))).toEqual({ username: 'ada', password: 'password123' });
		expect(new Headers(loginCalls[0].init.headers).has('Authorization')).toBe(false);
	});

	it('sends the workspace create and member-add bodies verbatim', async () => {
		const { fetcher, calls } = mockFetcher([
			jsonAnswer(201, { id: 'w1', slug: 'alpha', name: 'Alpha', role: 'owner', created_at: '2026-01-01T00:00:00Z' }),
			jsonAnswer(201, {
				workspace_id: 'w1',
				user_id: 'u2',
				username: 'bea',
				email: 'bea@example.com',
				role: 'editor',
				created_at: '2026-01-01T00:00:00Z'
			})
		]);
		const client = apiWithFetch(fetcher);

		await client.workspaces.create({ name: 'Alpha' });
		expect(calls[0].init.method).toBe('POST');
		expect(calls[0].url).toContain('/api/workspaces');
		expect(JSON.parse(String(calls[0].init.body))).toEqual({ name: 'Alpha' });

		await client.workspaces.members.add('w1', { user_id: 'u2', role: 'editor' });
		expect(calls[1].url).toContain('/api/workspaces/w1/members');
		expect(JSON.parse(String(calls[1].init.body))).toEqual({ user_id: 'u2', role: 'editor' });
	});

	it('names a target workspace on notebook create, import, and fork', async () => {
		const notebook = {
			id: 'n1',
			workspace_id: 'w1',
			created_by: 'u1',
			parent_id: null,
			title: 'T',
			description: null,
			tags: [],
			visibility: 'private',
			fork_count: 0,
			created_at: '2026-01-01T00:00:00Z',
			updated_at: '2026-01-01T00:00:00Z'
		};
		const { fetcher, calls } = mockFetcher(jsonAnswer(201, notebook));
		const client = apiWithFetch(fetcher);

		await client.notebooks.create({ title: 'T', workspace_id: 'w1' });
		expect(JSON.parse(String(calls[0].init.body))).toEqual({ title: 'T', workspace_id: 'w1' });

		await client.notebooks.import({ url: 'https://example.com/nb.py', workspace_id: 'w1' });
		expect(JSON.parse(String(calls[1].init.body))).toEqual({ url: 'https://example.com/nb.py', workspace_id: 'w1' });

		await client.notebooks.fork('n0', { workspace_id: 'w1' });
		expect(calls[2].url).toContain('/api/notebooks/n0/fork');
		expect(JSON.parse(String(calls[2].init.body))).toEqual({ workspace_id: 'w1' });
	});

	it('sends the visibility change body to the publish endpoint and returns the updated notebook', async () => {
		const notebook = {
			id: 'n1',
			workspace_id: 'w1',
			created_by: 'u1',
			parent_id: null,
			title: 'T',
			description: null,
			tags: [],
			visibility: 'public',
			fork_count: 0,
			created_at: '2026-01-01T00:00:00Z',
			updated_at: '2026-01-01T00:00:00Z'
		};
		const { fetcher, calls } = mockFetcher(jsonAnswer(200, notebook));
		const client = apiWithFetch(fetcher);

		const result = await client.notebooks.publish('n1', { visibility: 'public' });
		expect(calls[0].url).toContain('/api/notebooks/n1/publish');
		expect(calls[0].init.method).toBe('POST');
		expect(JSON.parse(String(calls[0].init.body))).toEqual({ visibility: 'public' });
		expect(result.visibility).toBe('public');
	});

	it('sends a bodyless DELETE for permanent notebook removal', async () => {
		const { fetcher, calls } = mockFetcher({ status: 204 });
		await apiWithFetch(fetcher).notebooks.delete('n1');
		expect(calls[0].url).toContain('/api/notebooks/n1');
		expect(calls[0].init.method).toBe('DELETE');
		expect(calls[0].init.body).toBeUndefined();
	});

	it('sends the session create body and lets teardown options reach the delete request', async () => {
		auth.setSession(fakeJwt('user-1'));
		try {
			const { fetcher, calls } = mockFetcher([
				jsonAnswer(201, { id: 's1', notebook_id: 'n1', mode: 'edit', proxy_url: '/api/sessions/s1/proxy/' }),
				{ status: 204 }
			]);
			const client = apiWithFetch(fetcher);

			await client.sessions.create({ notebook_id: 'n1', mode: 'edit' });
			expect(JSON.parse(String(calls[0].init.body))).toEqual({ notebook_id: 'n1', mode: 'edit' });

			await client.sessions.delete('s1', { auth: false, keepalive: true });
			expect(calls[1].init.method).toBe('DELETE');
			expect(calls[1].init.keepalive).toBe(true);
			expect(new Headers(calls[1].init.headers).has('Authorization')).toBe(false);
		} finally {
			auth.clear();
		}
	});

	it('attaches the bearer token by default when one is present', async () => {
		auth.setSession(fakeJwt('user-1'));
		try {
			const { fetcher, calls } = mockFetcher(jsonAnswer(200, []));
			await apiWithFetch(fetcher).workspaces.list();
			expect(new Headers(calls[0].init.headers).get('Authorization')).toBe(`Bearer ${getAuthToken()}`);
		} finally {
			auth.clear();
		}
	});
});

describe('obsolete API surface stays removed', () => {
	it('no longer exposes the explicit session-save or public data-client methods', () => {
		expect((api.sessions as Record<string, unknown>).save).toBeUndefined();
		expect((api.notebooks as Record<string, unknown>).postData).toBeUndefined();
		expect((api.notebooks as Record<string, unknown>).getData).toBeUndefined();
	});
});
