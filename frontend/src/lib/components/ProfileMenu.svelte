<script lang="ts">
	import { ChevronRight, FolderKanban, LogOut, Settings } from '@lucide/svelte';
	import type { AuthUser } from '$lib/stores/auth';

	type Props = {
		user: AuthUser;
		avatar?: string;
		onlogout: () => void;
	};

	let { user, avatar, onlogout }: Props = $props();
	let menu: HTMLDetailsElement;
	let open = $state(false);

	const initials = $derived(initialsFor(user.username));

	function initialsFor(name: string) {
		const parts = name.split(/[\s._-]+/).filter(Boolean);
		const letters = parts.length > 1 ? parts.map((part) => part[0]).join('') : name;
		return letters.slice(0, 2).toUpperCase();
	}

	function close() {
		open = false;
	}

	function logout() {
		close();
		onlogout();
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

<details class="relative" bind:this={menu} bind:open>
	<summary
		class="grid size-9 cursor-pointer list-none place-items-center overflow-hidden rounded-full bg-[#26333b] text-xs font-semibold text-white ring-offset-2 ring-offset-app-bg transition hover:ring-2 hover:ring-brand [&::-webkit-details-marker]:hidden"
		aria-label="Open profile menu"
	>
		{#if avatar}
			<img class="size-full object-cover" src={avatar} alt="" />
		{:else}
			{initials}
		{/if}
	</summary>

	{#if open}
		<div class="absolute right-0 top-[calc(100%+0.6rem)] z-50 w-72 overflow-hidden rounded-lg border border-app-line bg-app-raised shadow-[var(--shadow-popover)]">
			<div class="border-b border-app-line p-4">
				<div class="flex items-center gap-3">
					<span class="grid size-10 shrink-0 place-items-center overflow-hidden rounded-full bg-[#26333b] text-xs font-semibold text-white">
						{#if avatar}<img class="size-full object-cover" src={avatar} alt="" />{:else}{initials}{/if}
					</span>
					<div class="min-w-0">
						<p class="m-0 truncate text-sm font-semibold text-app-fg">{user.username}</p>
						{#if user.email}<p class="m-0 truncate text-xs text-app-muted">{user.email}</p>{/if}
					</div>
				</div>
			</div>
			<div class="p-1.5">
				<a
					href="/settings"
					onclick={close}
					class="flex items-center gap-3 rounded-md px-3 py-2.5 text-sm text-app-muted hover:bg-app-sidebar-hover hover:text-app-fg"
				>
					<Settings size={16} /><span class="flex-1">Profile settings</span><ChevronRight size={14} />
				</a>
				<a
					href="/workspaces"
					onclick={close}
					class="flex items-center gap-3 rounded-md px-3 py-2.5 text-sm text-app-muted hover:bg-app-sidebar-hover hover:text-app-fg"
				>
					<FolderKanban size={16} /><span class="flex-1">Your workspaces</span><ChevronRight size={14} />
				</a>
				<div class="my-1 border-t border-app-line"></div>
				<button
					type="button"
					onclick={logout}
					class="flex w-full items-center gap-3 rounded-md px-3 py-2.5 text-left text-sm text-app-muted hover:bg-app-sidebar-hover hover:text-app-danger"
				>
					<LogOut size={16} /> Log out
				</button>
			</div>
		</div>
	{/if}
</details>
