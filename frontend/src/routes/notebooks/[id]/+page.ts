import { error } from '@sveltejs/kit';
import { ApiError, apiWithFetch } from '$lib/api';
import type { PageLoad } from './$types';

export const load: PageLoad = async ({ fetch, params }) => {
	try {
		const notebook = await apiWithFetch(fetch).notebooks.get(params.id);

		return { notebook };
	} catch (caught) {
		if (caught instanceof ApiError) {
			throw error(caught.status, caught.detail || 'Notebook not found');
		}
		throw error(500, 'Unable to load notebook.');
	}
};
