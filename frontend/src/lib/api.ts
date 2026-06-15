import { env } from '$env/dynamic/public';
import { getAuthToken } from '$lib/stores/auth';

export type User = {
	id: string;
	username: string;
	email: string;
	created_at: string;
};

export type NotebookVisibility = 'draft' | 'unlisted' | 'public';

export type Notebook = {
	id: string;
	user_id: string;
	parent_id: string | null;
	title: string;
	description: string | null;
	tags: string[];
	visibility: NotebookVisibility;
	fork_count: number;
	source?: string;
	created_at: string;
	updated_at: string;
};

export type LoginResponse = { access_token: string; token_type: 'bearer' };
export type NotebookListResponse = { items: Notebook[]; total: number; page: number; page_size: number };
export type SessionMode = 'edit' | 'run';
export type SessionResponse = { id: string; notebook_id: string; mode: SessionMode; proxy_url: string };
export type DeploymentResponse = { slug: string; status: string; url: string };
export type NotebookDataResponse = { payload: unknown; source: string; created_at: string };
export type CreatedNotebookDataResponse = { id: string };

export type ApiError = Error & { status: number; detail: string };

type RequestOptions = Omit<RequestInit, 'body'> & { body?: unknown; token?: string | null };

const baseUrl = env.PUBLIC_API_URL ?? '';
export const mockApiEnabled = env.PUBLIC_MOCK_API === 'true' || (!baseUrl && env.PUBLIC_MOCK_API !== 'false');

export const fixtureNotebooks: Notebook[] = [
	{
		id: 'nb_molab_intro',
		user_id: 'user_demo',
		parent_id: null,
		title: 'MoLab getting started',
		description: 'A fixture notebook for frontend development before the backend is available.',
		tags: ['demo', 'marimo', 'phase-0'],
		visibility: 'public',
		fork_count: 7,
		source: 'https://github.com/demo/molab-intro.py',
		created_at: '2026-06-01T12:00:00.000Z',
		updated_at: '2026-06-10T12:00:00.000Z'
	},
	{
		id: 'nb_vector_search',
		user_id: 'user_demo',
		parent_id: null,
		title: 'Vector search playground',
		description: 'Mock search and tag metadata for Discover UI work.',
		tags: ['search', 'pgvector'],
		visibility: 'public',
		fork_count: 3,
		created_at: '2026-06-03T09:30:00.000Z',
		updated_at: '2026-06-12T16:45:00.000Z'
	}
];

function createApiError(status: number, detail: string): ApiError {
	const error = new Error(detail) as ApiError;
	error.status = status;
	error.detail = detail;
	return error;
}

function pathWithQuery(path: string, query?: Record<string, string | number | undefined>) {
	if (path.startsWith('http://') || path.startsWith('https://')) return path;
	const url = new URL(`${baseUrl}${path}`, baseUrl || 'http://localhost');
	for (const [key, value] of Object.entries(query ?? {})) {
		if (value !== undefined && value !== '') url.searchParams.set(key, String(value));
	}
	return baseUrl ? url.toString() : `${url.pathname}${url.search}`;
}

export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
	const { body, token = getAuthToken(), headers, ...init } = options;
	const response = await fetch(pathWithQuery(path), {
		...init,
		headers: {
			...(body === undefined ? {} : { 'content-type': 'application/json' }),
			...(token ? { authorization: `Bearer ${token}` } : {}),
			...headers
		},
		body: body === undefined ? undefined : JSON.stringify(body)
	});

	if (!response.ok) {
		let detail = response.statusText;
		try {
			const payload = (await response.json()) as { detail?: string };
			detail = payload.detail ?? detail;
		} catch {
			// Non-JSON errors still become the frozen {detail} shape for callers.
		}
		throw createApiError(response.status, detail);
	}

	if (response.status === 204) return undefined as T;
	return (await response.json()) as T;
}

async function mockOrFetch<T>(mockValue: T, fetcher: () => Promise<T>) {
	return mockApiEnabled ? mockValue : fetcher();
}

export const api = {
	register: (body: { username: string; email: string; password: string }) =>
		apiFetch<User>('/api/auth/register', { method: 'POST', body }),
	login: (body: { username: string; password: string }) =>
		apiFetch<LoginResponse>('/api/auth/login', { method: 'POST', body, token: null }),
	logout: () => apiFetch<void>('/api/auth/logout', { method: 'POST' }),
	listNotebooks: (query: { q?: string; tags?: string; semantic?: string; page?: number; page_size?: number } = {}) =>
		mockOrFetch(
			{ items: fixtureNotebooks, total: fixtureNotebooks.length, page: query.page ?? 1, page_size: query.page_size ?? 20 },
			() => apiFetch<NotebookListResponse>(pathWithQuery('/api/notebooks', query))
		),
	createNotebook: (body: { title: string; description?: string; tags?: string[]; source?: string }) =>
		apiFetch<Notebook>('/api/notebooks', { method: 'POST', body }),
	getNotebook: (id: string) => apiFetch<Notebook>(`/api/notebooks/${id}`, { token: getAuthToken() }),
	updateNotebook: (id: string, body: { title?: string; description?: string; tags?: string[]; source?: string }) =>
		apiFetch<Notebook>(`/api/notebooks/${id}`, { method: 'PUT', body }),
	deleteNotebook: (id: string) => apiFetch<void>(`/api/notebooks/${id}`, { method: 'DELETE' }),
	publishNotebook: (id: string, body: { visibility: NotebookVisibility }) =>
		apiFetch<Notebook>(`/api/notebooks/${id}/publish`, { method: 'POST', body }),
	forkNotebook: (id: string) => apiFetch<Notebook>(`/api/notebooks/${id}/fork`, { method: 'POST' }),
	importNotebook: (body: { url: string; pat?: string }) =>
		apiFetch<Notebook>('/api/notebooks/import', { method: 'POST', body }),
	createSession: (body: { notebook_id: string; mode: SessionMode }) =>
		apiFetch<SessionResponse>('/api/sessions', { method: 'POST', body }),
	deleteSession: (id: string) => apiFetch<void>(`/api/sessions/${id}`, { method: 'DELETE' }),
	deployNotebook: (id: string, body: { slug?: string }) =>
		apiFetch<DeploymentResponse>(`/api/notebooks/${id}/deploy`, { method: 'POST', body }),
	getDeployment: (slug: string) => apiFetch<Response>(`/api/deployments/${slug}`),
	deleteDeployment: (slug: string) => apiFetch<void>(`/api/deployments/${slug}`, { method: 'DELETE' }),
	createNotebookData: (id: string, body: unknown) =>
		apiFetch<CreatedNotebookDataResponse>(`/api/notebooks/${id}/data`, { method: 'POST', body, token: null }),
	getNotebookData: (id: string) => apiFetch<NotebookDataResponse>(`/api/notebooks/${id}/data`, { token: null })
};
