import { browser } from '$app/environment';
import { env } from '$env/dynamic/public';
import { getAuthToken } from '$lib/stores/auth';

export type User = {
	id: string;
	username: string;
	email: string;
	created_at: string;
};

export type Token = {
	access_token: string;
	token_type: 'bearer';
};

export type NotebookVisibility = 'draft' | 'unlisted' | 'public';

export type Notebook = {
	id: string;
	user_id: string;
	parent_id: string | null;
	parent_title?: string | null;
	parent_owner_id?: string | null;
	parent_owner_username?: string | null;
	title: string;
	description: string | null;
	tags: string[];
	visibility: NotebookVisibility;
	fork_count: number;
	source?: string;
	created_at: string;
	updated_at: string;
};

export type Paginated<T> = {
	items: T[];
	total: number;
	page: number;
	page_size: number;
};

export type RegisterRequest = {
	username: string;
	email: string;
	password: string;
};

export type LoginRequest = {
	username: string;
	password: string;
};

export type NotebookListParams = {
	q?: string;
	tags?: string | string[];
	semantic?: string;
	page?: number;
	page_size?: number;
};

export type NotebookCreateRequest = {
	title: string;
	description?: string | null;
	tags?: string[];
	source?: string;
};

export type NotebookUpdateRequest = Partial<NotebookCreateRequest>;

export type NotebookPublishRequest = {
	visibility: NotebookVisibility;
};

export type NotebookImportRequest = {
	url: string;
	pat?: string;
};

export type SessionMode = 'edit' | 'run';

export type SessionCreateRequest = {
	notebook_id: string;
	mode: SessionMode;
};

export type Session = {
	id: string;
	notebook_id: string;
	mode: SessionMode;
	proxy_url: string;
};

export type DeploymentStatus = 'running' | 'sleeping' | 'stopped';

export type DeploymentCreateRequest = {
	slug?: string;
};

export type Deployment = {
	slug: string;
	status: DeploymentStatus;
	url: string;
};

export type NotebookData = {
	payload: unknown;
	source: string;
	created_at: string;
};

export type NotebookDataCreated = {
	id: string;
};

export class ApiError extends Error {
	readonly status: number;
	readonly detail: string;

	constructor(status: number, detail: string) {
		super(detail);
		this.name = 'ApiError';
		this.status = status;
		this.detail = detail;
	}
}

type RequestOptions = Omit<RequestInit, 'body'> & {
	auth?: boolean;
	body?: unknown;
	fetch?: typeof fetch;
	query?: Record<string, string | number | boolean | string[] | undefined>;
};

const apiBaseUrl = normalizeApiBaseUrl(env.PUBLIC_API_URL ?? '');

function normalizeApiBaseUrl(value: string) {
	const trimmed = value.trim().replace(/\/$/, '');
	if (!trimmed) return '';

	let url: URL;
	try {
		url = new URL(trimmed);
	} catch {
		throw new Error('PUBLIC_API_URL must be an absolute http(s) URL');
	}

	if (url.protocol !== 'http:' && url.protocol !== 'https:') {
		throw new Error('PUBLIC_API_URL must be an absolute http(s) URL');
	}

	return trimmed;
}

function buildUrl(path: string, query?: RequestOptions['query']) {
	const normalizedPath = path.startsWith('/') ? path : `/${path}`;
	const url = new URL(`${apiBaseUrl}${normalizedPath}`, 'http://localhost');

	for (const [key, value] of Object.entries(query ?? {})) {
		if (value === undefined || value === '') continue;
		if (Array.isArray(value)) {
			if (value.length > 0) url.searchParams.set(key, value.join(','));
		} else {
			url.searchParams.set(key, String(value));
		}
	}

	return apiBaseUrl ? url.toString() : `${url.pathname}${url.search}`;
}

export function resolveApiUrl(value: string) {
	try {
		return new URL(value).toString();
	} catch {
		if (!apiBaseUrl) return value;
		return new URL(value, `${apiBaseUrl}/`).toString();
	}
}

export function deploymentProxyUrl(slug: string) {
	return resolveApiUrl(buildUrl(`/api/deployments/${slug}/`));
}

export function normalizeTagInput(tags: string | string[] | undefined) {
	const values = Array.isArray(tags) ? tags : tags?.split(',') ?? [];
	return values.map((tag) => tag.trim()).filter((tag) => tag.length > 0);
}

async function readError(response: Response) {
	try {
		const body = (await response.json()) as { detail?: unknown };
		if (typeof body.detail === 'string') return body.detail;
		if (Array.isArray(body.detail)) {
			const messages = body.detail
				.map((item) => {
					if (item && typeof item === 'object' && 'msg' in item) return String(item.msg);
					return undefined;
				})
				.filter(Boolean);
			if (messages.length > 0) return messages.join(', ');
		}
	} catch {
		return response.statusText || 'Request failed';
	}
	return response.statusText || 'Request failed';
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
	const { auth, body, fetch: fetcher = globalThis.fetch, query, ...init } = options;
	const headers = new Headers(init.headers);
	const token = getAuthToken();

	if (!apiBaseUrl && !browser && fetcher === globalThis.fetch) {
		throw new Error('PUBLIC_API_URL must be set for server-side live API requests without SvelteKit fetch');
	}

	if (auth !== false && token) headers.set('Authorization', `Bearer ${token}`);
	if (body !== undefined && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');

	const response = await fetcher(buildUrl(path, query), {
		...init,
		headers,
		body: body === undefined ? undefined : JSON.stringify(body)
	});

	if (!response.ok) throw new ApiError(response.status, await readError(response));
	if (response.status === 204) return undefined as T;

	return (await response.json()) as T;
}

function createLiveApi(fetcher?: typeof fetch) {
	const request = <T>(path: string, options: RequestOptions = {}) =>
		apiRequest<T>(path, fetcher ? { ...options, fetch: fetcher } : options);

	return {
		auth: {
			register: (body: RegisterRequest) => request<User>('/api/auth/register', { method: 'POST', auth: false, body }),
			login: (body: LoginRequest) => request<Token>('/api/auth/login', { method: 'POST', auth: false, body }),
			logout: () => request<void>('/api/auth/logout', { method: 'POST' })
		},
		notebooks: {
			list: (query: NotebookListParams = {}) =>
				request<Paginated<Notebook>>('/api/notebooks', { query: { ...query, tags: normalizeTagInput(query.tags) } }),
			create: (body: NotebookCreateRequest) => request<Notebook>('/api/notebooks', { method: 'POST', body }),
			get: (id: string) => request<Notebook>(`/api/notebooks/${id}`),
			update: (id: string, body: NotebookUpdateRequest) =>
				request<Notebook>(`/api/notebooks/${id}`, { method: 'PUT', body }),
			delete: (id: string) => request<void>(`/api/notebooks/${id}`, { method: 'DELETE' }),
			publish: (id: string, body: NotebookPublishRequest) =>
				request<Notebook>(`/api/notebooks/${id}/publish`, { method: 'POST', body }),
			fork: (id: string) => request<Notebook>(`/api/notebooks/${id}/fork`, { method: 'POST' }),
			import: (body: NotebookImportRequest) =>
				request<Notebook>('/api/notebooks/import', { method: 'POST', body }),
			deploy: (id: string, body: DeploymentCreateRequest = {}) =>
				request<Deployment>(`/api/notebooks/${id}/deploy`, { method: 'POST', body }),
			postData: (id: string, body: unknown) =>
				request<NotebookDataCreated>(`/api/notebooks/${id}/data`, { method: 'POST', auth: false, body }),
			getData: (id: string) => request<NotebookData>(`/api/notebooks/${id}/data`, { auth: false })
		},
		sessions: {
			create: (body: SessionCreateRequest) => request<Session>('/api/sessions', { method: 'POST', body }),
			save: (id: string) => request<void>(`/api/sessions/${id}/save`, { method: 'POST' }),
			delete: (id: string) => request<void>(`/api/sessions/${id}`, { method: 'DELETE' })
		},
		deployments: {
			get: async (slug: string) => {
				const headers = new Headers();
				const token = getAuthToken();
				if (token) headers.set('Authorization', `Bearer ${token}`);
				if (!apiBaseUrl && !browser && !fetcher) {
					throw new Error('PUBLIC_API_URL must be set for server-side live API requests without SvelteKit fetch');
				}

				const response = await (fetcher ?? globalThis.fetch)(buildUrl(`/api/deployments/${slug}`), { headers });
				if (!response.ok) throw new ApiError(response.status, await readError(response));
				return response;
			},
			delete: (slug: string) => request<void>(`/api/deployments/${slug}`, { method: 'DELETE' })
		}
	};
}

const liveApi = createLiveApi();

export function apiWithFetch(fetcher: typeof fetch) {
	return createLiveApi(fetcher);
}

export const api = liveApi;
