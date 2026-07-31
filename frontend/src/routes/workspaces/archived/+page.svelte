<script lang="ts">
	import { invalidateAll } from '$app/navigation';
	import { ApiError, api, type WorkspaceArchive } from '$lib/api';
	import { workspaces } from '$lib/stores/workspaces';
	import Button from '$lib/components/Button.svelte';

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

<section class="space-y-8">
	<Button intent="secondary" size="sm" class="w-fit" href="/workspaces">Back to workspaces</Button>

	<div>
		<h1 class="text-4xl font-black tracking-tight text-slate-950 dark:text-white sm:text-5xl">Archived workspaces</h1>
		<p class="mt-3 max-w-2xl text-lg leading-8 text-slate-700 dark:text-slate-300">
			Archiving is reversible. A workspace you own stays here, restorable, until its purge date — after that it is
			permanently removed the next time the scheduled purge job runs.
		</p>
	</div>

	{#if archived.length === 0}
		<p class="rounded-[2rem] border border-slate-900/10 bg-white/70 p-6 text-sm font-semibold text-slate-600 dark:border-white/10 dark:bg-white/10 dark:text-slate-300">
			You have no archived workspaces.
		</p>
	{:else}
		<div class="space-y-4">
			{#each archived as workspace (workspace.id)}
				<div class="rounded-[2rem] border border-slate-900/10 bg-white/80 p-6 shadow-lg shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20">
					<div class="flex flex-wrap items-center justify-between gap-4">
						<div>
							<h2 class="text-xl font-bold text-slate-950 dark:text-white">{workspace.name}</h2>
							<p class="mt-1 font-mono text-sm text-slate-500 dark:text-slate-400">{workspace.slug}</p>
						</div>
						<Button
							type="button"
							intent="secondary"
							size="sm"
							onclick={() => void restore(workspace)}
							disabled={restoringId === workspace.id}
						>
							{restoringId === workspace.id ? 'Restoring...' : 'Restore'}
						</Button>
					</div>
					<div class="mt-4 grid gap-3 border-t border-slate-900/10 pt-4 text-sm dark:border-white/10 sm:grid-cols-2">
						<div>
							<p class="font-semibold uppercase tracking-[0.16em] text-slate-500 dark:text-slate-400">Archived</p>
							<p class="mt-1 font-bold text-slate-950 dark:text-white">{formatDate(workspace.archived_at)}</p>
						</div>
						<div>
							<p class="font-semibold uppercase tracking-[0.16em] text-slate-500 dark:text-slate-400">Scheduled purge</p>
							<p class="mt-1 font-bold text-slate-950 dark:text-white">{formatDate(workspace.purge_after)}</p>
						</div>
					</div>
					<p class="mt-3 text-xs text-slate-500 dark:text-slate-400">
						Purge is permanent: the workspace, its notebooks, and their deployments cannot be recovered afterward.
					</p>
					{#if rowError?.id === workspace.id}
						<p class="mt-4 rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm font-semibold text-red-800 dark:border-red-400/20 dark:bg-red-500/10 dark:text-red-200" role="alert">{rowError.message}</p>
					{/if}
				</div>
			{/each}
		</div>
	{/if}
</section>
