import type { Workspace } from '$lib/api';
import { canWrite, type WorkspaceState } from '$lib/stores/workspaces';

export type NotebookCapability = 'hydrating' | 'write' | 'read';

// The workspace store only ever knows the caller's own memberships. Until it
// has settled at least once, "no role found" is ambiguous between "still
// loading" and "confirmed non-member" — collapsing those would flash
// read-only controls at an Owner before their role has even loaded.
export function notebookCapability(state: WorkspaceState, workspaceId: string): NotebookCapability {
	if (state.status === 'loading' && state.items.length === 0) return 'hydrating';
	return canWrite(state.items, workspaceId) ? 'write' : 'read';
}

// The caller's Workspace name/slug is only safe to show when it came from
// their own active membership list — a non-member reader must never see it,
// and there is no other endpoint that would resolve it for them.
export function notebookWorkspaceEntry(items: Workspace[], workspaceId: string): Workspace | null {
	return items.find((item) => item.id === workspaceId) ?? null;
}
