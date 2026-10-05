<script lang="ts">
	import '$lib/design/app.css';
	import {
		Archive,
		Compass,
		FilePlus,
		FolderKanban,
		House,
		LogIn,
		PanelLeft,
		PanelLeftClose,
		PanelLeftOpen,
		Search,
		Settings,
		X,
		type LucideIcon
	} from '@lucide/svelte';
	import { page } from '$app/state';
	import { afterNavigate, goto } from '$app/navigation';
	import { api } from '$lib/api';
	import BrandMark from '$lib/components/BrandMark.svelte';
	import Button from '$lib/components/Button.svelte';
	import ProfileMenu from '$lib/components/ProfileMenu.svelte';
	import ThemeSwitch from '$lib/components/ThemeSwitch.svelte';
	import WorkspaceMenu from '$lib/components/WorkspaceMenu.svelte';
	import { activeWorkspace } from '$lib/stores/activeWorkspace';
	import { auth, isAuthenticated } from '$lib/stores/auth';
	import { profileAvatars } from '$lib/stores/profile';
	import { workspaces } from '$lib/stores/workspaces';
	import { getInitialTheme } from '$lib/theme';

	type NavItem = { href: string; label: string; short?: string; icon: LucideIcon; requiresAuth?: boolean };

	const SIDEBAR_STORAGE_KEY = 'marimohub-sidebar-collapsed';

	const workspaceNav: NavItem[] = [
		{ href: '/', label: 'Home', icon: House },
		{ href: '/discover', label: 'Discover', icon: Compass },
		{ href: '/workspaces', label: 'Workspaces', icon: FolderKanban, requiresAuth: true },
		{ href: '/notebooks/new', label: 'New notebook', short: 'New', icon: FilePlus, requiresAuth: true }
	];
	const manageNav: NavItem[] = [
		{ href: '/settings', label: 'Settings', icon: Settings, requiresAuth: true },
		{ href: '/workspaces/archived', label: 'Archived workspaces', short: 'Archived', icon: Archive, requiresAuth: true }
	];

	let { children } = $props();

	let sidebarCollapsed = $state(false);
	let mobileOpen = $state(false);
	let railDialog = $state<HTMLDialogElement>();
	let searchInput = $state<HTMLInputElement>();
	let shortcutLabel = $state('Ctrl K');

	const pathname = $derived(page.url.pathname);
	// Edit/run host a full-height marimo iframe: no page padding, no page scroll,
	// and the sidebar held at its rail width so the notebook gets the room.
	const sessionRoute = $derived(/^\/notebooks\/[^/]+\/(edit|run)$/.test(pathname));
	// Sign-in, registration, the provider sign-in callback, and the public
	// Deployment viewer render without any application chrome.
	const bareRoute = $derived(
		pathname === '/auth/login' ||
			pathname === '/auth/register' ||
			pathname === '/auth/callback' ||
			pathname.startsWith('/deploy/')
	);
	const rail = $derived(sidebarCollapsed || sessionRoute);
	const signedIn = $derived(Boolean($auth.currentUser));

	const visibleWorkspaceNav = $derived(workspaceNav.filter((item) => signedIn || !item.requiresAuth));
	const visibleManageNav = $derived(manageNav.filter((item) => signedIn || !item.requiresAuth));
	const mobileNav = $derived(visibleWorkspaceNav.slice(0, 4));

	// The most specific matching item wins, so `/workspaces/archived` lights up
	// "Archived workspaces" rather than "Workspaces".
	const activeHref = $derived.by(() => {
		let best: string | null = null;
		for (const item of [...workspaceNav, ...manageNav]) {
			const matches = item.href === '/' ? pathname === '/' : pathname === item.href || pathname.startsWith(`${item.href}/`);
			if (matches && (!best || item.href.length > best.length)) best = item.href;
		}
		return best;
	});

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

	$effect(() => {
		getInitialTheme();
		try {
			sidebarCollapsed = localStorage.getItem(SIDEBAR_STORAGE_KEY) === 'true';
		} catch {
			sidebarCollapsed = false;
		}
		if (/Mac|iPhone|iPad/.test(navigator.platform)) shortcutLabel = '⌘ K';
	});

	afterNavigate(() => {
		mobileOpen = false;
		railDialog?.close();
	});

	function toggleSidebar() {
		sidebarCollapsed = !sidebarCollapsed;
		try {
			localStorage.setItem(SIDEBAR_STORAGE_KEY, String(sidebarCollapsed));
		} catch {
			// Storage can be unavailable in privacy-restricted contexts; the toggle still works for this page view.
		}
	}

	// From the rail: at desktop width a collapsed sidebar simply expands again;
	// anywhere the rail is all there is room for (or a session pins it), the full
	// navigation opens as an overlay instead.
	function expandNavigation() {
		if (!sessionRoute && sidebarCollapsed && window.matchMedia('(min-width: 80rem)').matches) {
			toggleSidebar();
			return;
		}
		railDialog?.showModal();
	}

	function onWindowKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape' && mobileOpen) mobileOpen = false;
		if (event.key.toLowerCase() === 'k' && (event.metaKey || event.ctrlKey) && searchInput && !bareRoute) {
			event.preventDefault();
			searchInput.focus();
		}
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
	<meta name="theme-color" content="#00c7ad" />
	<link rel="icon" href="/assets/marimohub-icon-light.svg" />
	<link rel="icon" href="/assets/marimohub-icon-light.svg" media="(prefers-color-scheme: light)" />
	<link rel="icon" href="/assets/marimohub-icon-dark-transparent.svg" media="(prefers-color-scheme: dark)" />
</svelte:head>

<svelte:window onkeydown={onWindowKeydown} />

{#snippet navLink(item: NavItem, compact: boolean, onclick?: () => void)}
	{@const Icon = item.icon}
	{@const here = activeHref === item.href}
	<a
		href={item.href}
		{onclick}
		class={`mb-0.5 flex items-center rounded-md py-2 text-[13px] font-medium no-underline transition ${compact ? 'justify-center px-2' : 'justify-center px-2 xl:justify-start xl:gap-2.5 xl:px-2.5'} ${here ? 'bg-brand-soft text-brand-strong' : 'text-app-muted hover:bg-app-sidebar-hover hover:text-app-fg'}`}
		aria-current={here ? 'page' : undefined}
		aria-label={item.label}
		title={item.label}
	>
		<Icon size={17} strokeWidth={here ? 2.3 : 1.9} />
		{#if !compact}<span class="hidden xl:inline">{item.label}</span>{/if}
	</a>
{/snippet}

{#snippet overlayLink(item: NavItem, onclick: () => void)}
	{@const Icon = item.icon}
	{@const here = activeHref === item.href}
	<a
		href={item.href}
		{onclick}
		class={`mb-0.5 flex items-center gap-2.5 rounded-md px-2.5 py-2 text-[13px] font-medium no-underline transition ${here ? 'bg-brand-soft text-brand-strong' : 'text-app-muted hover:bg-app-sidebar-hover hover:text-app-fg'}`}
		aria-current={here ? 'page' : undefined}
	>
		<Icon size={17} strokeWidth={here ? 2.3 : 1.9} />{item.label}
	</a>
{/snippet}

{#if bareRoute}
	<a class="skip-link" href="#main">Skip to content</a>
	{@render children()}
{:else}
	<a class="skip-link" href="#main">Skip to content</a>

	<div class={sessionRoute ? 'h-dvh overflow-hidden bg-app-bg text-app-fg' : 'min-h-dvh bg-app-bg text-app-fg'}>
		<aside
			class={`fixed inset-y-0 left-0 z-40 hidden flex-col border-r border-app-line bg-app-sidebar transition-[width] duration-200 md:flex ${rail ? 'w-[68px]' : 'w-[68px] xl:w-[248px]'}`}
			aria-label="Application sidebar"
		>
			<div class={`relative flex h-16 shrink-0 items-center border-b border-app-line ${rail ? 'justify-center px-2' : 'justify-center px-2 xl:justify-start xl:gap-2.5 xl:px-4'}`}>
				<a href="/" class="flex items-center gap-2.5 rounded-md text-app-fg no-underline" aria-label="MarimoHub home" title="MarimoHub home">
					<BrandMark />
					{#if !rail}<span class="hidden text-[17px] font-semibold tracking-[-0.02em] xl:inline">MarimoHub</span>{/if}
				</a>
				{#if !rail}
					<button
						type="button"
						onclick={toggleSidebar}
						class="ml-auto hidden size-8 place-items-center rounded-md text-app-muted hover:bg-app-sidebar-hover hover:text-app-fg xl:grid"
						aria-label="Collapse sidebar"
						title="Collapse sidebar"
					>
						<PanelLeftClose size={17} />
					</button>
				{/if}
				<button
					type="button"
					onclick={expandNavigation}
					class={`absolute -right-3 top-5 z-10 size-6 place-items-center rounded-full border border-app-line bg-app-card text-app-muted shadow-[var(--shadow-sm)] hover:border-app-line-strong hover:text-app-fg ${rail ? 'grid' : 'grid xl:hidden'}`}
					aria-label="Expand navigation"
					title="Expand navigation"
				>
					<PanelLeftOpen size={13} />
				</button>
			</div>

			<nav class={`flex min-h-0 flex-1 flex-col overflow-y-auto py-3 ${rail ? 'px-2' : 'px-2 xl:px-2.5'}`} aria-label="Primary navigation">
				{#if !rail}
					<p class="m-0 hidden px-2 pb-1.5 text-[10px] font-semibold uppercase tracking-[0.11em] text-app-muted xl:block">Workspace</p>
				{/if}
				{#each visibleWorkspaceNav as item (item.href)}
					{@render navLink(item, rail)}
				{/each}

				{#if visibleManageNav.length > 0}
					{#if !rail}
						<p class="m-0 mt-5 hidden px-2 pb-1.5 text-[10px] font-semibold uppercase tracking-[0.11em] text-app-muted xl:block">Manage</p>
					{/if}
					<div class={`mx-2 my-3 border-t border-app-line ${rail ? '' : 'xl:hidden'}`}></div>
					{#each visibleManageNav as item (item.href)}
						{@render navLink(item, rail)}
					{/each}
				{/if}
			</nav>

			{#if !rail}
				<div class="hidden shrink-0 border-t border-app-line p-2.5 xl:block">
					<p class="m-0 flex items-center gap-2 px-2.5 py-2 text-[11px] leading-4 text-app-muted">
						<span class="size-1.5 shrink-0 rounded-full bg-brand"></span>
						Forkable marimo notebooks for research, demos, and apps
					</p>
				</div>
			{/if}
		</aside>

		<dialog
			bind:this={railDialog}
			aria-label="Expanded navigation"
			class={`fixed inset-y-0 left-0 z-50 m-0 hidden h-dvh max-h-none w-[288px] max-w-none flex-col border-0 border-r border-app-line bg-app-sidebar p-0 text-app-fg shadow-[var(--shadow-popover)] open:flex backdrop:bg-black/45 ${sessionRoute ? '' : 'xl:open:hidden'}`}
		>
			<div class="flex h-16 shrink-0 items-center gap-2.5 border-b border-app-line px-4">
				<BrandMark />
				<span class="text-[17px] font-semibold tracking-[-0.02em]">MarimoHub</span>
				<button
					type="button"
					onclick={() => railDialog?.close()}
					class="ml-auto grid size-8 place-items-center rounded-md text-app-muted hover:bg-app-sidebar-hover hover:text-app-fg"
					aria-label="Close expanded navigation"><X size={17} /></button
				>
			</div>
			<nav class="flex min-h-0 flex-1 flex-col overflow-y-auto px-2.5 py-3" aria-label="Expanded primary navigation">
				<p class="m-0 px-2 pb-1.5 text-[10px] font-semibold uppercase tracking-[0.11em] text-app-muted">Workspace</p>
				{#each visibleWorkspaceNav as item (item.href)}
					{@render overlayLink(item, () => railDialog?.close())}
				{/each}
				{#if visibleManageNav.length > 0}
					<p class="m-0 mt-5 px-2 pb-1.5 text-[10px] font-semibold uppercase tracking-[0.11em] text-app-muted">Manage</p>
					{#each visibleManageNav as item (item.href)}
						{@render overlayLink(item, () => railDialog?.close())}
					{/each}
				{/if}
			</nav>
		</dialog>

		<header
			class={`fixed inset-x-0 top-0 z-30 flex h-16 items-center gap-2 border-b border-app-line bg-app-bg/95 px-3 backdrop-blur transition-[left] duration-200 md:left-[68px] md:px-6 ${rail ? '' : 'xl:left-[248px]'}`}
		>
			<button
				type="button"
				onclick={() => (mobileOpen = !mobileOpen)}
				class="grid size-9 shrink-0 place-items-center rounded-md text-app-muted hover:bg-app-sidebar-hover md:hidden"
				aria-label="Toggle navigation"
				aria-expanded={mobileOpen}
			>
				<PanelLeft size={19} />
			</button>
			<a href="/" class="flex shrink-0 items-center rounded-md no-underline md:hidden" aria-label="MarimoHub home">
				<BrandMark size="sm" />
			</a>

			<form action="/discover" method="GET" role="search" class="ml-2 hidden w-full max-w-xl sm:block md:ml-0">
				<label class="relative block">
					<span class="visually-hidden">Search notebooks</span>
					<Search size={16} class="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-app-muted" />
					<input
						bind:this={searchInput}
						name="q"
						type="search"
						placeholder="Search notebooks by title or description…"
						class="h-9 w-full rounded-md border border-app-line bg-app-card pl-9 pr-16 text-[13px] text-app-fg outline-none transition placeholder:text-app-muted focus:border-brand-strong"
					/>
					<kbd class="pointer-events-none absolute right-2.5 top-1/2 hidden -translate-y-1/2 rounded border border-app-line bg-app-bg px-1.5 py-0.5 font-sans text-[10px] text-app-muted lg:block">{shortcutLabel}</kbd>
				</label>
			</form>

			<div class="ml-auto flex items-center gap-2 sm:pl-5">
				{#if $auth.currentUser}
					<WorkspaceMenu items={$workspaces.items} activeId={$activeWorkspace} onselect={(id) => void selectWorkspace(id)} />
				{/if}
				<ThemeSwitch />
				{#if $auth.currentUser}
					<ProfileMenu user={$auth.currentUser} avatar={$profileAvatars[$auth.currentUser.id]} onlogout={() => void logout()} />
				{:else}
					<Button intent="primary" href="/auth/login"><LogIn size={15} /> Sign in</Button>
				{/if}
			</div>
		</header>

		{#if mobileOpen}
			<button class="fixed inset-0 z-40 bg-black/35 md:hidden" aria-label="Close navigation" onclick={() => (mobileOpen = false)}></button>
			<div class="fixed inset-y-0 left-0 z-50 flex w-[280px] flex-col border-r border-app-line bg-app-sidebar p-3 shadow-[var(--shadow-popover)] md:hidden">
				<div class="mb-3 flex items-center gap-2.5 px-2 py-2">
					<BrandMark />
					<span class="text-lg font-semibold tracking-[-0.02em]">MarimoHub</span>
					<button
						type="button"
						onclick={() => (mobileOpen = false)}
						class="ml-auto grid size-8 place-items-center rounded-md text-app-muted hover:bg-app-sidebar-hover hover:text-app-fg"
						aria-label="Close navigation menu"><X size={17} /></button
					>
				</div>
				<form action="/discover" method="GET" role="search" class="mb-3 px-1">
					<label class="relative block">
						<span class="visually-hidden">Search notebooks</span>
						<Search size={15} class="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-app-muted" />
						<input
							name="q"
							type="search"
							placeholder="Search notebooks…"
							class="h-9 w-full rounded-md border border-app-line bg-app-card pl-9 pr-3 text-[13px] text-app-fg outline-none placeholder:text-app-muted focus:border-brand-strong"
						/>
					</label>
				</form>
				<nav class="grid gap-1" aria-label="Navigation menu">
					{#each [...visibleWorkspaceNav, ...visibleManageNav] as item (item.href)}
						{@const Icon = item.icon}
						<a
							href={item.href}
							onclick={() => (mobileOpen = false)}
							class={`flex items-center gap-3 rounded-md px-3 py-2.5 text-sm no-underline ${activeHref === item.href ? 'bg-brand-soft text-brand-strong' : 'text-app-muted hover:bg-app-sidebar-hover hover:text-app-fg'}`}
							aria-current={activeHref === item.href ? 'page' : undefined}
						>
							<Icon size={18} />
							{item.label}
						</a>
					{/each}
				</nav>
			</div>
		{/if}

		{#if sessionRoute}
			<main id="main" class="flex h-dvh min-h-0 flex-col pt-16 md:pl-[68px]">
				{@render children()}
			</main>
		{:else}
			<main id="main" class={`min-h-dvh pt-16 transition-[padding] duration-200 md:pl-[68px] ${rail ? '' : 'xl:pl-[248px]'}`}>
				<div class="mx-auto w-full max-w-[1540px] px-4 py-6 pb-24 sm:px-6 md:pb-10 lg:px-8 lg:py-8">
					{@render children()}
				</div>
			</main>

			<nav
				class="fixed inset-x-0 bottom-0 z-30 grid border-t border-app-line bg-app-card px-1 pb-[env(safe-area-inset-bottom)] md:hidden"
				style:grid-template-columns={`repeat(${mobileNav.length + (signedIn ? 0 : 1)}, minmax(0, 1fr))`}
				aria-label="Mobile navigation"
			>
				{#each mobileNav as item (item.href)}
					{@const Icon = item.icon}
					<a
						href={item.href}
						class={`flex flex-col items-center gap-1 px-1 py-2 text-[10px] font-medium no-underline ${activeHref === item.href ? 'text-brand-strong' : 'text-app-muted'}`}
						aria-current={activeHref === item.href ? 'page' : undefined}
					>
						<Icon size={18} />
						{item.short ?? item.label}
					</a>
				{/each}
				{#if !signedIn}
					<a href="/auth/login" class="flex flex-col items-center gap-1 px-1 py-2 text-[10px] font-medium text-app-muted no-underline">
						<LogIn size={18} />
						Sign in
					</a>
				{/if}
			</nav>
		{/if}
	</div>
{/if}
