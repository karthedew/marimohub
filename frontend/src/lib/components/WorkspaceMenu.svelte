<script lang="ts">
	import type { Workspace } from '$lib/api';

	type Props = {
		items: Workspace[];
		activeId: string | null;
		onselect: (workspaceId: string) => void;
	};

	let { items, activeId, onselect }: Props = $props();
	let menu: HTMLDetailsElement;
	const active = $derived(items.find((workspace) => workspace.id === activeId) ?? null);

	function close() {
		menu.open = false;
	}

	function select(workspaceId: string) {
		close();
		onselect(workspaceId);
	}

	$effect(() => {
		function dismiss(event: PointerEvent | KeyboardEvent) {
			if (event instanceof KeyboardEvent && event.key !== 'Escape') return;
			if (event instanceof PointerEvent && menu.contains(event.target as Node)) return;
			close();
		}
		document.addEventListener('pointerdown', dismiss);
		document.addEventListener('keydown', dismiss);
		return () => {
			document.removeEventListener('pointerdown', dismiss);
			document.removeEventListener('keydown', dismiss);
		};
	});
</script>

<details class="group relative min-w-0" bind:this={menu}>
	<summary
		class="flex max-w-[15rem] cursor-pointer list-none items-center gap-2 rounded-xl border border-slate-900/10 bg-white px-3 py-2 text-left text-sm font-semibold text-slate-800 shadow-sm outline-none transition hover:bg-slate-50 focus-visible:ring-4 focus-visible:ring-hub-600/20 dark:border-white/10 dark:bg-[#181a1b] dark:text-slate-100 dark:hover:bg-[#202324] [&::-webkit-details-marker]:hidden"
	>
		<span class="grid size-6 shrink-0 place-items-center rounded-md bg-hub-700 text-[0.65rem] font-black text-white">
			{active?.name.slice(0, 1).toUpperCase() ?? 'W'}
		</span>
		<span class="min-w-0 flex-1 truncate">{active?.name ?? 'Choose workspace'}</span>
		<svg class="size-4 shrink-0 text-slate-400 transition group-open:rotate-180" viewBox="0 0 20 20" fill="none" aria-hidden="true">
			<path d="m6 8 4 4 4-4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" />
		</svg>
	</summary>

	<div class="absolute left-0 z-50 mt-2 w-72 max-w-[calc(100vw-2rem)] overflow-hidden rounded-2xl border border-slate-900/10 bg-white p-2 shadow-2xl shadow-slate-950/15 dark:border-white/10 dark:bg-[#181a1b] dark:shadow-black/40">
		<p class="px-3 pb-2 pt-1 text-[0.68rem] font-bold uppercase tracking-[0.16em] text-slate-400">Workspaces</p>
		{#if items.length === 0}
			<p class="px-3 py-2 text-sm text-slate-500 dark:text-slate-400">Create a Workspace to start.</p>
		{:else}
			{#each items as workspace (workspace.id)}
				<button
					type="button"
					class={`flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left text-sm transition ${workspace.id === activeId ? 'bg-slate-100 text-slate-950 dark:bg-white/10 dark:text-white' : 'text-slate-600 hover:bg-slate-50 hover:text-slate-950 dark:text-slate-300 dark:hover:bg-white/5 dark:hover:text-white'}`}
					onclick={() => select(workspace.id)}
				>
					<span class="grid size-7 shrink-0 place-items-center rounded-lg bg-slate-200 text-xs font-black text-slate-700 dark:bg-slate-700 dark:text-slate-200">{workspace.name.slice(0, 1).toUpperCase()}</span>
					<span class="min-w-0 flex-1">
						<span class="block truncate font-semibold">{workspace.name}</span>
						<span class="block text-xs capitalize text-slate-400">{workspace.role}</span>
					</span>
					{#if workspace.id === activeId}
						<svg class="size-4 shrink-0 text-hub-700 dark:text-hub-300" viewBox="0 0 20 20" fill="none" aria-hidden="true">
							<path d="m4.5 10.5 3.25 3.25 7.75-8" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
						</svg>
					{/if}
				</button>
			{/each}
		{/if}

		<div class="mt-2 border-t border-slate-900/10 pt-2 dark:border-white/10">
			<a class="flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-semibold text-slate-600 transition hover:bg-slate-50 hover:text-slate-950 dark:text-slate-300 dark:hover:bg-white/5 dark:hover:text-white" href="/workspaces?create=1" onclick={close}>
				<span class="grid size-7 place-items-center rounded-lg border border-dashed border-slate-400 text-lg font-normal">+</span>
				Create workspace
			</a>
			{#if active}
				<a class="block rounded-xl px-3 py-2 text-sm font-semibold text-slate-500 transition hover:bg-slate-50 hover:text-slate-950 dark:text-slate-400 dark:hover:bg-white/5 dark:hover:text-white" href={`/workspaces/${active.id}`} onclick={close}>Manage workspace</a>
			{/if}
		</div>
	</div>
</details>
