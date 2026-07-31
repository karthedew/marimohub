import { error } from '@sveltejs/kit';
import { ApiError, apiWithFetch } from '$lib/api';
import { requireAuth } from '$lib/routeGuards';
import type { PageLoad } from './$types';

export const load: PageLoad = async ({ fetch, params, url }) => {
	requireAuth(url);

	try {
		const client = apiWithFetch(fetch);
		const [workspace, members] = await Promise.all([
			client.workspaces.get(params.id),
			client.workspaces.members.list(params.id)
		]);

		return { workspace, members };
	} catch (caught) {
		if (caught instanceof ApiError) {
			throw error(caught.status, caught.detail || 'Workspace not found');
		}
		throw error(500, 'Unable to load workspace.');
	}
};
