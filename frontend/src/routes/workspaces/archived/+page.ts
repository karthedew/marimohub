import { error } from '@sveltejs/kit';
import { ApiError, apiWithFetch } from '$lib/api';
import { requireAuth } from '$lib/routeGuards';
import type { PageLoad } from './$types';

export const load: PageLoad = async ({ fetch, url }) => {
	requireAuth(url);

	try {
		const archived = await apiWithFetch(fetch).workspaces.listArchived();
		return { archived };
	} catch (caught) {
		if (caught instanceof ApiError) {
			throw error(caught.status, caught.detail || 'Unable to load archived workspaces.');
		}
		throw error(500, 'Unable to load archived workspaces.');
	}
};
