import { browser } from '$app/environment';
import { goto } from '$app/navigation';
import { env } from '$env/dynamic/public';
import { auth, getAuthToken } from '$lib/stores/auth';

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

export type WorkspaceRole = 'owner' | 'editor' | 'viewer';

export type Workspace = {
	id: string;
	slug: string;
	name: string;
	role: WorkspaceRole;
	created_at: string;
};

export type WorkspaceArchive = Workspace & {
	archived_at: string;
	purge_after: string;
};

export type WorkspaceMember = {
	workspace_id: string;
	user_id: string;
	username: string;
	email: string;
	role: WorkspaceRole;
	created_at: string;
};

export type WorkspaceCreateRequest = {
	name: string;
	slug?: string;
};

export type WorkspaceRenameRequest = {
	name: string;
};

export type WorkspaceMemberCreateRequest = {
	user_id: string;
	role?: WorkspaceRole;
};

export type WorkspaceMemberUpdateRequest = {
	role: WorkspaceRole;
};

export type NotebookVisibility = 'private' | 'unlisted' | 'public';

export type Notebook = {
	id: string;
	workspace_id: string;
	created_by: string | null;
	parent_id: string | null;
	parent_title?: string | null;
	parent_workspace_id?: string | null;
	parent_workspace_slug?: string | null;
	title: string;
	description: string | null;
	tags: string[];
	visibility: NotebookVisibility;
	fork_count: number;
	source?: string | null;
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
	workspace_id?: string;
	page?: number;
	page_size?: number;
};

export type NotebookCreateRequest = {
	title: string;
	description?: string | null;
	tags?: string[];
	source?: string | null;
	workspace_id: string;
};

// Never advertises workspace reassignment: the backend has no notebook-move
// operation, so this type intentionally has no `workspace_id` field.
export type NotebookUpdateRequest = {
	title?: string;
	description?: string | null;
	tags?: string[];
	source?: string | null;
};

export type NotebookPublishRequest = {
	visibility: NotebookVisibility;
};

export type NotebookImportRequest = {
	url: string;
	pat?: string;
	workspace_id: string;
};

export type NotebookForkRequest = {
	workspace_id: string;
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
	// Skips the centralized expired-session redirect for this one request.
	// Logout uses this: it already clears local state itself, and letting the
	// generic handler race a redirect against logout's own navigation could
	// send the user back to the page they just asked to leave.
	handle401?: boolean;
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
			for (const item of value) {
				if (item === '') continue;
				url.searchParams.append(key, item);
			}
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

export async function readError(response: Response) {
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

function isAuthRoutePath(pathname: string) {
	return pathname === '/auth/login' || pathname === '/auth/register';
}

// The one place a bearer 401 gets turned into a redirect. Only fires when
// this request actually sent a token: an anonymous request that happens to
// 401 (e.g. a notebook that requires sign-in) is not evidence the session
// expired, so it must never clear a session that never existed.
function expireSession() {
	auth.clear();
	if (!browser) return;
	const { pathname, search } = window.location;
	if (isAuthRoutePath(pathname)) return;
	void goto(`/auth/login?next=${encodeURIComponent(`${pathname}${search}`)}`);
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
	const {
		auth: sendAuth,
		handle401 = true,
		body,
		fetch: fetcher = globalThis.fetch,
		query,
		...init
	} = options;
	const headers = new Headers(init.headers);
	const token = getAuthToken();
	const tokenSent = sendAuth !== false && Boolean(token);

	if (!apiBaseUrl && !browser && fetcher === globalThis.fetch) {
		throw new Error('PUBLIC_API_URL must be set for server-side live API requests without SvelteKit fetch');
	}

	if (tokenSent) headers.set('Authorization', `Bearer ${token}`);
	if (body !== undefined && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');

	const response = await fetcher(buildUrl(path, query), {
		...init,
		headers,
		body: body === undefined ? undefined : JSON.stringify(body)
	});

	if (response.status === 401 && tokenSent && handle401) expireSession();

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
			logout: () => request<void>('/api/auth/logout', { method: 'POST', handle401: false })
		},
		workspaces: {
			list: () => request<Workspace[]>('/api/workspaces'),
			create: (body: WorkspaceCreateRequest) => request<Workspace>('/api/workspaces', { method: 'POST', body }),
			get: (id: string) => request<Workspace>(`/api/workspaces/${id}`),
			rename: (id: string, body: WorkspaceRenameRequest) =>
				request<Workspace>(`/api/workspaces/${id}`, { method: 'PATCH', body }),
			archive: (id: string) => request<void>(`/api/workspaces/${id}`, { method: 'DELETE' }),
			restore: (id: string) => request<Workspace>(`/api/workspaces/${id}/restore`, { method: 'POST' }),
			listArchived: () => request<WorkspaceArchive[]>('/api/workspaces/archived'),
			members: {
				list: (workspaceId: string) => request<WorkspaceMember[]>(`/api/workspaces/${workspaceId}/members`),
				add: (workspaceId: string, body: WorkspaceMemberCreateRequest) =>
					request<WorkspaceMember>(`/api/workspaces/${workspaceId}/members`, { method: 'POST', body }),
				updateRole: (workspaceId: string, userId: string, body: WorkspaceMemberUpdateRequest) =>
					request<WorkspaceMember>(`/api/workspaces/${workspaceId}/members/${userId}`, { method: 'PATCH', body }),
				remove: (workspaceId: string, userId: string) =>
					request<void>(`/api/workspaces/${workspaceId}/members/${userId}`, { method: 'DELETE' })
			}
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
			fork: (id: string, body: NotebookForkRequest) =>
				request<Notebook>(`/api/notebooks/${id}/fork`, { method: 'POST', body }),
			import: (body: NotebookImportRequest) =>
				request<Notebook>('/api/notebooks/import', { method: 'POST', body }),
			deploy: (id: string, body: DeploymentCreateRequest = {}) =>
				request<Deployment>(`/api/notebooks/${id}/deploy`, { method: 'POST', body }),
			getDeployment: (id: string) => request<Deployment>(`/api/notebooks/${id}/deployment`)
		},
		sessions: {
			create: (body: SessionCreateRequest) => request<Session>('/api/sessions', { method: 'POST', body }),
			delete: (id: string, options: Omit<RequestOptions, 'method' | 'body' | 'query'> = {}) =>
				request<void>(`/api/sessions/${id}`, { method: 'DELETE', ...options })
		},
		deployments: {
			// Not routed through `request`/`apiRequest`: the response body is
			// whatever the proxied marimo app returns (HTML, in the common case),
			// not JSON, so this only needs the raw `Response` to check `.ok` —
			// parsing it as JSON on success would throw for every real deployment.
			get: async (slug: string) => {
				const activeFetcher = fetcher ?? globalThis.fetch;
				if (!apiBaseUrl && !browser && !fetcher) {
					throw new Error('PUBLIC_API_URL must be set for server-side live API requests without SvelteKit fetch');
				}

				const headers = new Headers();
				const token = getAuthToken();
				if (token) headers.set('Authorization', `Bearer ${token}`);

				const response = await activeFetcher(buildUrl(`/api/deployments/${slug}`), { headers });
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
