<script lang="ts">
	import '../app.css';
	import { auth } from '$lib/stores/auth';
	import { applyTheme, getInitialTheme, type Theme } from '$lib/theme';
	import favicon from '$lib/assets/favicon.svg';

	let { children } = $props();
	let theme = $state<Theme>('light');

	$effect(() => {
		theme = getInitialTheme();
	});

	function toggleTheme() {
		theme = theme === 'dark' ? 'light' : 'dark';
		applyTheme(theme);
	}
</script>

<svelte:head>
	<link rel="icon" href={favicon} />
</svelte:head>

<div class="min-h-screen bg-[radial-gradient(circle_at_top_left,_rgba(249,115,22,0.18),_transparent_32rem),linear-gradient(180deg,_#f7f4ec,_#fffaf0)] text-ink transition-colors dark:bg-[radial-gradient(circle_at_top_left,_rgba(249,115,22,0.16),_transparent_30rem),linear-gradient(180deg,_#0b1120,_#111827)] dark:text-slate-50">
	<header class="mx-auto flex w-full max-w-6xl items-center justify-between px-5 py-5 sm:px-8">
		<a href="/" class="flex items-center gap-3 font-semibold tracking-tight" aria-label="MoLab home">
			<span class="grid size-10 place-items-center rounded-2xl bg-graphite text-lg text-white shadow-lg shadow-orange-950/10 dark:bg-molten">Mo</span>
			<span class="text-xl">MoLab</span>
		</a>

		<nav class="flex items-center gap-3 text-sm font-medium sm:gap-5">
			<a class="rounded-full px-3 py-2 text-slate-700 hover:bg-white/70 dark:text-slate-200 dark:hover:bg-white/10" href="/discover">Discover</a>
			{#if $auth.currentUser}
				<span class="hidden rounded-full bg-white/70 px-3 py-2 text-slate-700 dark:bg-white/10 dark:text-slate-200 sm:inline">{$auth.currentUser.username}</span>
			{:else}
				<a class="rounded-full bg-graphite px-4 py-2 text-white shadow-sm dark:bg-white dark:text-slate-950" href="/auth/login">Login</a>
			{/if}
			<button
				class="rounded-full border border-slate-300/70 bg-white/60 px-3 py-2 text-slate-700 backdrop-blur hover:bg-white dark:border-white/15 dark:bg-white/10 dark:text-slate-100 dark:hover:bg-white/15"
				type="button"
				aria-label="Toggle color theme"
				aria-pressed={theme === 'dark'}
				onclick={toggleTheme}
			>
				{theme === 'dark' ? 'Light' : 'Dark'}
			</button>
		</nav>
	</header>

	<main class="mx-auto w-full max-w-6xl px-5 pb-16 pt-8 sm:px-8">
		{@render children()}
	</main>

	<footer class="border-t border-slate-900/10 px-5 py-8 text-center text-sm text-slate-600 dark:border-white/10 dark:text-slate-400">
		MoLab hosts forkable marimo notebooks for research, demos, and deployed apps.
	</footer>
</div>
