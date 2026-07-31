<script lang="ts">
	import '../app.css';
	import { page } from '$app/state';
	import { goto } from '$app/navigation';
	import { api } from '$lib/api';
	import ProfileMenu from '$lib/components/ProfileMenu.svelte';
	import ThemeSwitch from '$lib/components/ThemeSwitch.svelte';
	import WorkspaceMenu from '$lib/components/WorkspaceMenu.svelte';
	import { activeWorkspace } from '$lib/stores/activeWorkspace';
	import { auth, isAuthenticated } from '$lib/stores/auth';
	import { profileAvatars } from '$lib/stores/profile';
	import { workspaces } from '$lib/stores/workspaces';
	import { applyTheme, getInitialTheme, type Theme } from '$lib/theme';
	import Button from '$lib/components/Button.svelte';

	let { children } = $props();
	let theme = $state<Theme>('light');
	const sessionRoute = $derived(/\/notebooks\/[^/]+\/(edit|run)$/.test(page.url.pathname));

	// The active workspace list has exactly one owner: this effect. It mirrors
	// every place identity can change — initial storage hydration, login,
	// register, logout, and expiry — because all of them are just transitions
	// of `$auth.currentUser.id` through this one reactive block.
	let previousUserId: string | null = null;
	$effect(() => {
		const userId = $auth.currentUser?.id ?? null;
		if (userId) {
			if (userId !== previousUserId) void workspaces.refresh();
		} else if (previousUserId) {
			workspaces.clear();
			activeWorkspace.clear();
		}
		previousUserId = userId;
	});

	$effect(() => {
		function onFocus() {
			if (isAuthenticated()) void workspaces.refresh();
		}
		window.addEventListener('focus', onFocus);
		return () => window.removeEventListener('focus', onFocus);
	});

	$effect(() => {
		if ($workspaces.status === 'ready') activeWorkspace.reconcile($workspaces.items);
	});
	const shellClass = $derived(
		sessionRoute
			? 'flex h-screen min-h-0 flex-col overflow-hidden bg-slate-50 text-ink transition-colors dark:bg-[#101213] dark:text-slate-50'
			: 'flex min-h-screen flex-col bg-slate-50 text-ink transition-colors dark:bg-[#101213] dark:text-slate-50'
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
			// Local session state is authoritative for this client regardless of
			// whether the backend accepted the request (e.g. an already-expired
			// token). This request also opts out of the centralized 401 redirect
			// (see `handle401` in `$lib/api`) so it can never race this function's
			// own navigation back to the page being logged out from.
		} finally {
			auth.clear();
			workspaces.clear();
		}
		await goto('/');
	}

	async function selectWorkspace(workspaceId: string) {
		activeWorkspace.set(workspaceId);
		await goto(`/?workspace=${workspaceId}`);
	}
</script>

<svelte:head>
	<link rel="icon" href="/assets/marimohub-icon-light.svg" />
	<link rel="icon" href="/assets/marimohub-icon-light.svg" media="(prefers-color-scheme: light)" />
	<link rel="icon" href="/assets/marimohub-icon-dark-transparent.svg" media="(prefers-color-scheme: dark)" />
</svelte:head>


<div class={shellClass}>
	<header class="z-40 shrink-0 border-b border-slate-900/10 bg-white/90 backdrop-blur-xl dark:border-white/10 dark:bg-[#101213]/90">
		<div class="mx-auto flex min-h-16 w-full max-w-7xl flex-wrap items-center gap-3 px-4 py-2 sm:flex-nowrap sm:px-6 lg:px-8">
			<a href="/" class="mr-auto flex shrink-0 items-center sm:mr-2" aria-label="MarimoHub home">
				<img class="h-8 w-auto dark:hidden" src="/assets/marimohub-lockup-light.svg" alt="MarimoHub" />
				<img class="hidden h-8 w-auto dark:block" src="/assets/marimohub-lockup-dark-transparent.svg" alt="MarimoHub" />
			</a>

			{#if $auth.currentUser}
				<div class="order-3 w-full sm:order-none sm:w-auto">
					<WorkspaceMenu items={$workspaces.items} activeId={$activeWorkspace} onselect={(id) => void selectWorkspace(id)} />
				</div>
			{/if}

			<nav class="ml-auto flex items-center gap-1.5 text-sm font-medium">
				<Button intent="ghost" size="sm" href="/discover">Discover</Button>
				<ThemeSwitch {theme} onchange={toggleTheme} />
				{#if $auth.currentUser}
					<ProfileMenu user={$auth.currentUser} avatar={$profileAvatars[$auth.currentUser.id]} onlogout={() => void logout()} />
				{:else}
					<Button intent="primary" size="sm" href="/auth/login">Login</Button>
				{/if}
			</nav>
		</div>
	</header>

	<main class={sessionRoute ? 'flex min-h-0 w-full flex-1 flex-col' : 'mx-auto w-full max-w-7xl flex-1 px-4 pb-16 pt-8 sm:px-6 lg:px-8'}>
		{@render children()}
	</main>

	{#if !sessionRoute}
		<footer class="border-t border-slate-900/10 px-5 py-7 text-center text-sm text-slate-500 dark:border-white/10 dark:text-slate-500">
			MarimoHub hosts forkable marimo notebooks for research, demos, and deployed apps.
		</footer>
	{/if}
</div>
