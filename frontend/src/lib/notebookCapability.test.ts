import { describe, expect, it } from 'vitest';

import type { Workspace } from '$lib/api';
import type { WorkspaceState } from '$lib/stores/workspaces';
import { notebookCapability, notebookWorkspaceEntry } from './notebookCapability';

function workspace(id: string, role: Workspace['role']): Workspace {
	return { id, slug: id, name: id, role, created_at: '2026-01-01T00:00:00Z' };
}

describe('notebookCapability', () => {
	it('reports hydrating while the store has never loaded anything yet', () => {
		const state: WorkspaceState = { status: 'loading', items: [] };
		expect(notebookCapability(state, 'w1')).toBe('hydrating');
	});

	it('reports write for an owner or editor of the notebook workspace', () => {
		expect(notebookCapability({ status: 'ready', items: [workspace('w1', 'owner')] }, 'w1')).toBe('write');
		expect(notebookCapability({ status: 'ready', items: [workspace('w1', 'editor')] }, 'w1')).toBe('write');
	});

	it('reports read for a viewer of the notebook workspace', () => {
		expect(notebookCapability({ status: 'ready', items: [workspace('w1', 'viewer')] }, 'w1')).toBe('read');
	});

	it('reports read for a non-member once the store has settled, never a permission error state', () => {
		expect(notebookCapability({ status: 'ready', items: [] }, 'w1')).toBe('read');
		expect(notebookCapability({ status: 'anonymous', items: [] }, 'w1')).toBe('read');
	});

	it('reports read, not hydrating, once a background refresh has stale items to fall back on', () => {
		const state: WorkspaceState = { status: 'loading', items: [workspace('w1', 'viewer')] };
		expect(notebookCapability(state, 'w1')).toBe('read');
	});
});

describe('notebookWorkspaceEntry', () => {
	it('returns the matching membership entry', () => {
		const items = [workspace('w1', 'owner'), workspace('w2', 'viewer')];
		expect(notebookWorkspaceEntry(items, 'w2')).toEqual(workspace('w2', 'viewer'));
	});

	it('returns null when the caller has no membership in that workspace', () => {
		expect(notebookWorkspaceEntry([workspace('w1', 'owner')], 'w2')).toBeNull();
	});
});
