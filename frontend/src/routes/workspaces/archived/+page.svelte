<script lang="ts">
	import { invalidateAll } from '$app/navigation';
	import { ApiError, api, type WorkspaceArchive } from '$lib/api';
	import { workspaces } from '$lib/stores/workspaces';
	import { Archive, ArchiveRestore, TriangleAlert } from '@lucide/svelte';
	import Button from '$lib/components/Button.svelte';
	import { card, errorBanner } from '$lib/design/classes';
	import Breadcrumbs from '$lib/design/components/Breadcrumbs.svelte';
	import Callout from '$lib/design/components/Callout.svelte';
	import EmptyState from '$lib/design/components/EmptyState.svelte';
	import PageHeader from '$lib/design/components/PageHeader.svelte';

	let { data } = $props();

	// Derived from load data rather than a local copy: restore reloads `data`
	// through `invalidateAll()` so this list always reflects what the backend
	// actually still has archived, never a hand-patched guess.
	const archived = $derived(data.archived);
	let restoringId = $state<string | null>(null);
	let rowError = $state<{ id: string; message: string } | null>(null);

	function formatDate(value: string) {
		return new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric', year: 'numeric' }).format(new Date(value));
	}

	async function restore(workspace: WorkspaceArchive) {
		rowError = null;
		restoringId = workspace.id;
		try {
			await api.workspaces.restore(workspace.id);
			await Promise.all([invalidateAll(), workspaces.refresh()]);
		} catch (caught) {
			rowError = { id: workspace.id, message: caught instanceof ApiError ? caught.detail : 'Unable to restore workspace.' };
		} finally {
			restoringId = null;
		}
	}
</script>

<svelte:head>
	<title>Archived workspaces | MarimoHub</title>
</svelte:head>

<Breadcrumbs crumbs={[{ label: 'Workspaces', href: '/workspaces' }, { label: 'Archived', href: '/workspaces/archived' }]} />

<PageHeader
	title="Archived workspaces"
	eyebrow="Archive"
	icon={Archive}
	description="Archiving is reversible. A workspace you own stays here, restorable, until its purge date — after that it is permanently removed the next time the scheduled purge job runs."
/>

{#if archived.length === 0}
	<EmptyState icon={Archive} title="You have no archived workspaces." detail="Workspaces you archive appear here until they are purged." />
{:else}
	<div class="overflow-hidden {card}">
		<div class="hidden grid-cols-[minmax(0,1fr)_140px_140px_120px] gap-3 border-b border-app-line bg-app-bg px-4 py-2.5 text-[10px] font-semibold uppercase tracking-[0.09em] text-app-muted md:grid">
			<span>Workspace</span><span>Archived</span><span>Scheduled purge</span><span></span>
		</div>
		{#each archived as workspace (workspace.id)}
			<div class="border-b border-app-line px-4 py-3 last:border-0">
				<div class="grid gap-3 md:grid-cols-[minmax(0,1fr)_140px_140px_120px] md:items-center">
					<div class="flex min-w-0 items-center gap-3">
						<span class="grid size-9 shrink-0 place-items-center rounded-md border border-app-line bg-app-bg text-xs font-bold text-app-muted">{workspace.name.slice(0, 2).toUpperCase()}</span>
						<div class="min-w-0">
							<p class="m-0 truncate text-sm font-semibold">{workspace.name}</p>
							<p class="m-0 truncate font-mono text-xs text-app-muted">{workspace.slug}</p>
						</div>
					</div>
					<p class="m-0 text-xs text-app-muted"><span class="font-medium text-app-fg md:hidden">Archived </span>{formatDate(workspace.archived_at)}</p>
					<p class="m-0 text-xs text-app-muted"><span class="font-medium text-app-fg md:hidden">Purge </span>{formatDate(workspace.purge_after)}</p>
					<div class="md:text-right">
						<Button type="button" intent="secondary" size="sm" onclick={() => void restore(workspace)} disabled={restoringId === workspace.id}>
							<ArchiveRestore size={14} />{restoringId === workspace.id ? 'Restoring...' : 'Restore'}
						</Button>
					</div>
				</div>
				{#if rowError?.id === workspace.id}
					<p class="{errorBanner} mt-3" role="alert">{rowError.message}</p>
				{/if}
			</div>
		{/each}
	</div>
	<Callout kind="warn" class="mt-4 flex items-start gap-2">
		<TriangleAlert size={16} class="mt-0.5 shrink-0 text-app-warn" />
		<span>Purge is permanent: the workspace, its notebooks, and their deployments cannot be recovered afterward.</span>
	</Callout>
{/if}
