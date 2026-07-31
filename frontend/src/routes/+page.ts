import { ApiError, apiWithFetch } from '$lib/api';
import { activeWorkspace, getActiveWorkspaceId } from '$lib/stores/activeWorkspace';
import { isAuthenticated } from '$lib/stores/auth';
import type { PageLoad } from './$types';

export const load: PageLoad = async ({ fetch, url }) => {
	if (!isAuthenticated()) return { authenticated: false as const };

	const client = apiWithFetch(fetch);
	try {
		const workspaces = await client.workspaces.list();
		const requestedId = url.searchParams.get('workspace');
		const candidateId = requestedId ?? getActiveWorkspaceId();
		const workspace = workspaces.find((item) => item.id === candidateId) ?? workspaces[0] ?? null;
		activeWorkspace.set(workspace?.id ?? null);

		const notebooks = workspace
			? await client.notebooks.list({ workspace_id: workspace.id, page_size: 100 })
			: { items: [], total: 0, page: 1, page_size: 100 };

		return { authenticated: true as const, workspace, notebooks, error: null };
	} catch (caught) {
		return {
			authenticated: true as const,
			workspace: null,
			notebooks: { items: [], total: 0, page: 1, page_size: 100 },
			error: caught instanceof ApiError ? caught.detail : 'Unable to load your notebooks.'
		};
	}
};
