import { api } from '$lib/api';

export async function load() {
	const notebooks = await api.listNotebooks();
	return { notebooks };
}
