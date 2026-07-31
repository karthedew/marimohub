import { requireAuth } from '$lib/routeGuards';
import type { PageLoad } from './$types';

export const load: PageLoad = ({ url }) => {
	requireAuth(url);
};
