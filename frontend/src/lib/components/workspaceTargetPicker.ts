import type { Workspace } from '$lib/api';
import { writableWorkspaces, type WorkspaceState } from '$lib/stores/workspaces';

export type TargetPickerState =
	| { kind: 'loading' }
	| { kind: 'error'; message: string }
	| { kind: 'empty' }
	| { kind: 'single'; workspace: Workspace }
	| { kind: 'multiple'; workspaces: Workspace[] };

// A first load with nothing cached yet blocks on loading/error; a background
// refresh (focus, retry) that already has last-known items falls straight
// through to the selection rules below instead of flashing a blocking state
// over data the caller has already seen.
export function targetPickerState(state: WorkspaceState): TargetPickerState {
	if (state.items.length === 0) {
		if (state.status === 'loading') return { kind: 'loading' };
		if (state.status === 'error') return { kind: 'error', message: state.error };
	}

	const writable = writableWorkspaces(state.items);
	if (writable.length === 0) return { kind: 'empty' };
	if (writable.length === 1) return { kind: 'single', workspace: writable[0] };
	return { kind: 'multiple', workspaces: writable };
}

// The one writable target is always the value — there is nothing to choose.
// With more than one, a value already picked (e.g. across a tab switch) is
// kept only while it still names a workspace in the current list; otherwise
// the picker never invents a default, so a real choice is always required.
export function nextTargetValue(state: TargetPickerState, currentValue: string): string {
	if (state.kind === 'single') return state.workspace.id;
	if (state.kind === 'multiple' && state.workspaces.some((workspace) => workspace.id === currentValue)) {
		return currentValue;
	}
	return '';
}

export function targetOptionLabel(workspace: Workspace, candidates: Workspace[]): string {
	const nameCollision = candidates.some((other) => other.id !== workspace.id && other.name === workspace.name);
	return nameCollision ? `${workspace.name} (${workspace.slug})` : workspace.name;
}
