<script lang="ts">
	import { Check, ChevronsUpDown, FolderCog, FolderKanban, Plus } from '@lucide/svelte';
	import type { Workspace } from '$lib/api';

	type Props = {
		items: Workspace[];
		activeId: string | null;
		onselect: (workspaceId: string) => void;
	};

	let { items, activeId, onselect }: Props = $props();
	let menu: HTMLDetailsElement;
	let open = $state(false);
	const active = $derived(items.find((workspace) => workspace.id === activeId) ?? null);

	function close() {
		open = false;
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

<details class="relative min-w-0" bind:this={menu} bind:open>
	<summary
		class="flex h-9 cursor-pointer list-none items-center gap-2 rounded-md border border-app-line bg-app-card px-1.5 text-left transition hover:border-app-line-strong lg:px-2 [&::-webkit-details-marker]:hidden"
		aria-label={active ? `Switch workspace, current: ${active.name}` : 'Choose workspace'}
		title={active ? `Workspace: ${active.name}` : 'Choose workspace'}
	>
		<span class="grid size-6 shrink-0 place-items-center rounded-md bg-brand-soft text-brand-strong">
			<FolderKanban size={14} strokeWidth={2.2} />
		</span>
		<span class="hidden min-w-0 max-w-40 truncate text-sm font-medium text-app-fg lg:block">{active?.name ?? 'Choose workspace'}</span>
		<ChevronsUpDown size={14} class="shrink-0 text-app-muted" />
	</summary>

	{#if open}
		<div class="absolute right-0 top-[calc(100%+0.45rem)] z-50 w-72 max-w-[calc(100vw-1.5rem)] overflow-hidden rounded-lg border border-app-line bg-app-raised p-1.5 shadow-[var(--shadow-popover)]">
			<p class="m-0 px-2.5 pb-1.5 pt-1 text-[10px] font-semibold uppercase tracking-[0.1em] text-app-muted">Switch workspace</p>
			{#if items.length === 0}
				<p class="m-0 px-2.5 py-2 text-sm text-app-muted">Create a Workspace to start.</p>
			{:else}
				{#each items as workspace (workspace.id)}
					<button
						type="button"
						class="flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-left hover:bg-app-sidebar-hover"
						class:bg-app-sidebar-hover={workspace.id === activeId}
						aria-current={workspace.id === activeId ? 'true' : undefined}
						onclick={() => select(workspace.id)}
					>
						<span class="grid size-8 shrink-0 place-items-center rounded-md border border-app-line bg-app-card text-xs font-bold text-app-muted">
							{workspace.name.slice(0, 2).toUpperCase()}
						</span>
						<span class="min-w-0 flex-1">
							<span class="block truncate text-sm font-medium text-app-fg">{workspace.name}</span>
							<span class="block truncate text-xs capitalize text-app-muted">{workspace.role}</span>
						</span>
						{#if workspace.id === activeId}<Check size={16} class="shrink-0 text-brand-strong" />{/if}
					</button>
				{/each}
			{/if}

			<div class="my-1 border-t border-app-line"></div>
			<a
				class="flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm text-app-muted hover:bg-app-sidebar-hover hover:text-app-fg"
				href="/workspaces?create=1"
				onclick={close}
			>
				<span class="grid size-8 place-items-center rounded-md border border-dashed border-app-line-strong"><Plus size={15} /></span>
				Create workspace
			</a>
			{#if active}
				<a
					class="flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm text-app-muted hover:bg-app-sidebar-hover hover:text-app-fg"
					href={`/workspaces/${active.id}`}
					onclick={close}
				>
					<span class="grid size-8 place-items-center rounded-md"><FolderCog size={15} /></span>
					Manage workspace
				</a>
			{/if}
		</div>
	{/if}
</details>
