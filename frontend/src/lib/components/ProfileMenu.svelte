<script lang="ts">
	import type { AuthUser } from '$lib/stores/auth';

	type Props = {
		user: AuthUser;
		avatar?: string;
		onlogout: () => void;
	};

	let { user, avatar, onlogout }: Props = $props();
	let menu: HTMLDetailsElement;
	const initial = $derived(user.username.slice(0, 1).toUpperCase());

	function close() {
		menu.open = false;
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

<details class="group relative" bind:this={menu}>
	<summary class="grid size-9 cursor-pointer list-none place-items-center overflow-hidden rounded-full bg-slate-900 text-sm font-black text-white outline-none ring-2 ring-transparent transition hover:ring-hub-600/25 focus-visible:ring-4 focus-visible:ring-hub-600/25 dark:bg-slate-100 dark:text-slate-950 [&::-webkit-details-marker]:hidden" aria-label="Open profile menu">
		{#if avatar}
			<img class="size-full object-cover" src={avatar} alt="" />
		{:else}
			{initial}
		{/if}
	</summary>

	<div class="absolute right-0 z-50 mt-2 w-64 overflow-hidden rounded-2xl border border-slate-900/10 bg-white p-2 shadow-2xl shadow-slate-950/15 dark:border-white/10 dark:bg-[#181a1b] dark:shadow-black/40">
		<div class="border-b border-slate-900/10 px-3 py-3 dark:border-white/10">
			<p class="truncate text-sm font-bold text-slate-950 dark:text-white">{user.username}</p>
			{#if user.email}<p class="mt-0.5 truncate text-xs text-slate-500 dark:text-slate-400">{user.email}</p>{/if}
		</div>
		<a class="mt-2 block rounded-xl px-3 py-2.5 text-sm font-semibold text-slate-600 transition hover:bg-slate-50 hover:text-slate-950 dark:text-slate-300 dark:hover:bg-white/5 dark:hover:text-white" href="/settings" onclick={close}>Profile settings</a>
		<button class="block w-full rounded-xl px-3 py-2.5 text-left text-sm font-semibold text-slate-600 transition hover:bg-slate-50 hover:text-slate-950 dark:text-slate-300 dark:hover:bg-white/5 dark:hover:text-white" type="button" onclick={logout}>Log out</button>
	</div>
</details>
