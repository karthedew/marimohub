<script lang="ts">
	import '../app.css';
	import { page } from '$app/state';
	import { goto } from '$app/navigation';
	import { api } from '$lib/api';
	import { auth } from '$lib/stores/auth';
	import { applyTheme, getInitialTheme, type Theme } from '$lib/theme';
	import Button from '$lib/components/Button.svelte';

	let { children } = $props();
	let theme = $state<Theme>('light');
	const sessionRoute = $derived(/\/notebooks\/[^/]+\/(edit|run)$/.test(page.url.pathname));
	const shellClass = $derived(
		sessionRoute
			? 'flex h-screen min-h-0 flex-col overflow-hidden bg-[radial-gradient(circle_at_top_left,_rgba(0,135,121,0.18),_transparent_32rem),linear-gradient(180deg,_#f2fbf9,_#f8fffd)] text-ink transition-colors dark:bg-[radial-gradient(circle_at_top_left,_rgba(0,135,121,0.24),_transparent_30rem),linear-gradient(180deg,_#061816,_#111827)] dark:text-slate-50'
			: 'flex min-h-screen flex-col bg-[radial-gradient(circle_at_top_left,_rgba(0,135,121,0.18),_transparent_32rem),linear-gradient(180deg,_#f2fbf9,_#f8fffd)] text-ink transition-colors dark:bg-[radial-gradient(circle_at_top_left,_rgba(0,135,121,0.24),_transparent_30rem),linear-gradient(180deg,_#061816,_#111827)] dark:text-slate-50'
	);

	$effect(() => {
		theme = getInitialTheme();
	});

	function toggleTheme() {
		theme = theme === 'dark' ? 'light' : 'dark';
		applyTheme(theme);
	}

	async function logout() {
		try {
			await api.auth.logout();
		} catch {
			// Local session state is the source of truth for this client.
		}
		auth.clear();
		await goto('/');
	}
</script>

<svelte:head>
	<link rel="icon" href="/assets/marimohub-icon-light.svg" />
	<link rel="icon" href="/assets/marimohub-icon-light.svg" media="(prefers-color-scheme: light)" />
	<link rel="icon" href="/assets/marimohub-icon-dark-transparent.svg" media="(prefers-color-scheme: dark)" />
</svelte:head>


<div class={shellClass}>
	<header class="mx-auto flex w-full max-w-6xl shrink-0 flex-col gap-4 px-5 py-5 sm:px-8 md:flex-row md:items-center md:justify-between">
		<a href="/" class="flex w-fit items-center" aria-label="MarimoHub home">
			<img class="h-12 w-auto dark:hidden" src="/assets/marimohub-lockup-light.svg" alt="MarimoHub" />
			<img class="hidden h-12 w-auto dark:block" src="/assets/marimohub-lockup-dark-transparent.svg" alt="MarimoHub" />
		</a>

		<nav class="flex flex-wrap items-center gap-2 text-sm font-medium sm:gap-3 md:justify-end">
			<Button intent="ghost" size="sm" href="/discover">Discover</Button>
			{#if $auth.currentUser}
				<span class="hidden rounded-full bg-white/70 px-3 py-2 text-slate-700 dark:bg-white/10 dark:text-slate-200 sm:inline">{$auth.currentUser.username}</span>
				<Button intent="primary" size="sm" type="button" onclick={logout}>Logout</Button>
			{:else}
				<Button intent="primary" size="sm" href="/auth/login">Login</Button>
			{/if}
			<Button
				intent="secondary"
				size="sm"
				type="button"
				aria-label="Toggle color theme"
				aria-pressed={theme === 'dark'}
				onclick={toggleTheme}
			>
				{theme === 'dark' ? 'Light' : 'Dark'}
			</Button>
		</nav>
	</header>

	<main class={sessionRoute ? 'flex min-h-0 w-full flex-1 flex-col px-0 pb-0 pt-0' : 'mx-auto w-full max-w-6xl flex-1 px-5 pb-16 pt-8 sm:px-8'}>
		{@render children()}
	</main>

	{#if !sessionRoute}
		<footer class="border-t border-slate-900/10 px-5 py-8 text-center text-sm text-slate-600 dark:border-white/10 dark:text-slate-400">
			MarimoHub hosts forkable marimo notebooks for research, demos, and deployed apps.
		</footer>
	{/if}
</div>
