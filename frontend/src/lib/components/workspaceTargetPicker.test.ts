import { describe, expect, it } from 'vitest';

import type { Workspace } from '$lib/api';
import type { WorkspaceState } from '$lib/stores/workspaces';
import { nextTargetValue, targetOptionLabel, targetPickerState, type TargetPickerState } from './workspaceTargetPicker';

function workspace(id: string, role: Workspace['role'] = 'owner', name = id): Workspace {
	return { id, slug: id, name, role, created_at: '2026-01-01T00:00:00Z' };
}

describe('targetPickerState', () => {
	it('reports loading only while there is nothing cached yet', () => {
		const state: WorkspaceState = { status: 'loading', items: [] };
		expect(targetPickerState(state)).toEqual({ kind: 'loading' });
	});

	it('falls through to the item-derived state during a background refresh with stale items', () => {
		const state: WorkspaceState = { status: 'loading', items: [workspace('w1')] };
		expect(targetPickerState(state)).toEqual({ kind: 'single', workspace: workspace('w1') });
	});

	it('reports the retryable error only while there is nothing cached yet', () => {
		const state: WorkspaceState = { status: 'error', items: [], error: 'boom' };
		expect(targetPickerState(state)).toEqual({ kind: 'error', message: 'boom' });
	});

	it('reports empty when the caller has no writable workspace', () => {
		const state: WorkspaceState = { status: 'ready', items: [workspace('w1', 'viewer')] };
		expect(targetPickerState(state)).toEqual({ kind: 'empty' });
	});

	it('reports empty for an authenticated caller with zero workspaces', () => {
		const state: WorkspaceState = { status: 'ready', items: [] };
		expect(targetPickerState(state)).toEqual({ kind: 'empty' });
	});

	it('reports single for exactly one writable workspace, ignoring viewer-only ones', () => {
		const state: WorkspaceState = {
			status: 'ready',
			items: [workspace('w1', 'owner'), workspace('w2', 'viewer')]
		};
		expect(targetPickerState(state)).toEqual({ kind: 'single', workspace: workspace('w1', 'owner') });
	});

	it('reports multiple for more than one writable workspace', () => {
		const state: WorkspaceState = {
			status: 'ready',
			items: [workspace('w1', 'owner'), workspace('w2', 'editor'), workspace('w3', 'viewer')]
		};
		expect(targetPickerState(state)).toEqual({
			kind: 'multiple',
			workspaces: [workspace('w1', 'owner'), workspace('w2', 'editor')]
		});
	});
});

describe('nextTargetValue', () => {
	it('forces the sole writable workspace regardless of the current value', () => {
		const state = { kind: 'single', workspace: workspace('w1') } as const;
		expect(nextTargetValue(state, '')).toBe('w1');
		expect(nextTargetValue(state, 'stale')).toBe('w1');
	});

	it('never invents a default among multiple targets', () => {
		const state: TargetPickerState = { kind: 'multiple', workspaces: [workspace('w1'), workspace('w2')] };
		expect(nextTargetValue(state, '')).toBe('');
	});

	it('keeps a deliberate selection across re-renders as long as it is still valid', () => {
		const state: TargetPickerState = { kind: 'multiple', workspaces: [workspace('w1'), workspace('w2')] };
		expect(nextTargetValue(state, 'w2')).toBe('w2');
	});

	it('drops a selection that fell out of the writable list', () => {
		const state: TargetPickerState = { kind: 'multiple', workspaces: [workspace('w1'), workspace('w2')] };
		expect(nextTargetValue(state, 'w3')).toBe('');
	});

	it('clears the value for loading, error, and empty states', () => {
		expect(nextTargetValue({ kind: 'loading' }, 'w1')).toBe('');
		expect(nextTargetValue({ kind: 'error', message: 'boom' }, 'w1')).toBe('');
		expect(nextTargetValue({ kind: 'empty' }, 'w1')).toBe('');
	});
});

describe('targetOptionLabel', () => {
	it('uses the plain name when it is unique among the candidates', () => {
		const candidates = [workspace('w1', 'owner', 'Research'), workspace('w2', 'owner', 'Ops')];
		expect(targetOptionLabel(candidates[0], candidates)).toBe('Research');
	});

	it('disambiguates a duplicate name with the immutable slug', () => {
		const candidates = [workspace('w1', 'owner', 'Research'), workspace('w2', 'owner', 'Research')];
		expect(targetOptionLabel(candidates[0], candidates)).toBe('Research (w1)');
		expect(targetOptionLabel(candidates[1], candidates)).toBe('Research (w2)');
	});
});
