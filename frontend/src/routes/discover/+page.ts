import { ApiError, apiWithFetch, normalizeTagInput } from '$lib/api';
import type { PageLoad } from './$types';

const pageSize = 6;

export const load: PageLoad = async ({ fetch, url }) => {
	const q = url.searchParams.get('q')?.trim() ?? '';
	const tagFilters = normalizeTagInput(url.searchParams.get('tags') ?? undefined);
	const tags = tagFilters.join(', ');
	const semantic = url.searchParams.get('semantic') === '1';
	const requestedPage = Number(url.searchParams.get('page') ?? '1');
	const page = Number.isFinite(requestedPage) && requestedPage > 0 ? Math.floor(requestedPage) : 1;

	try {
		const notebooks = await apiWithFetch(fetch).notebooks.list({
			page,
			page_size: pageSize,
			tags: tagFilters,
			q: semantic ? undefined : q || undefined,
			semantic: semantic && q ? q : undefined
		});

		return { notebooks, filters: { q, tags, semantic }, error: null };
	} catch (error) {
		return {
			notebooks: { items: [], total: 0, page, page_size: pageSize },
			filters: { q, tags, semantic },
			error: error instanceof ApiError ? error.detail : 'Unable to load notebooks.'
		};
	}
};
