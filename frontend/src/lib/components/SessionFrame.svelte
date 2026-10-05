<script lang="ts">
	import { beforeNavigate, goto } from '$app/navigation';
	import { page } from '$app/state';
	import { ApiError, api, resolveApiUrl, type Session, type SessionMode } from '$lib/api';
	import { teardownSessionOnUnload } from '$lib/sessionTeardown';
	import { ArrowLeft, CircleAlert, LoaderCircle, Lock, Play, RotateCcw } from '@lucide/svelte';
	import Button from '$lib/components/Button.svelte';

	type Props = {
		notebookId: string;
		mode: SessionMode;
		heading: string;
		backHref: string;
		backLabel: string;
	};

	type ErrorKind = 'forbidden' | 'notfound' | 'generic';

	let { notebookId, mode, heading, backHref, backLabel }: Props = $props();
	let session = $state<Session | null>(null);
	let loading = $state(true);
	let frameLoading = $state(false);
	let error = $state<string | null>(null);
	let errorKind = $state<ErrorKind>('generic');
	let active = false;
	let ending = $state(false);

	const iframeSrc = $derived(session ? resolveApiUrl(session.proxy_url) : '');
	const actionLabel = $derived(mode === 'edit' ? 'edit' : 'run');

	function loginRedirect() {
		const next = `${page.url.pathname}${page.url.search}`;
		return goto(`/auth/login?next=${encodeURIComponent(next)}`);
	}

	async function startSession() {
		loading = true;
		session = null;
		frameLoading = false;
		error = null;
		errorKind = 'generic';
		try {
			const created = await api.sessions.create({ notebook_id: notebookId, mode });
			if (!active) {
				await api.sessions.delete(created.id);
				return;
			}
			session = created;
			frameLoading = true;
			loading = false;
		} catch (caught) {
			if (!active) return;
			if (caught instanceof ApiError && caught.status === 401) {
				// No token was sent at all — an authenticated-but-expired token is
				// already redirected by the generic 401 handler in `api.ts`. Leave
				// `loading` as-is so the spinner keeps showing through the redirect
				// instead of flashing an "ended" state first.
				await loginRedirect();
				return;
			}
			if (caught instanceof ApiError && caught.status === 403) {
				errorKind = 'forbidden';
				error = 'Editor role is required to edit this notebook.';
			} else if (caught instanceof ApiError && caught.status === 404) {
				errorKind = 'notfound';
				error = caught.detail;
			} else {
				errorKind = 'generic';
				error = caught instanceof ApiError ? caught.detail : `Unable to start ${actionLabel} session.`;
			}
			loading = false;
		}
	}

	// The one teardown path: the End Session button, the in-app navigation
	// interceptor, and the component's own unmount all funnel through this.
	async function endSession() {
		if (!session || ending) return;
		ending = true;
		const endingSession = session;
		session = null;
		frameLoading = false;
		try {
			await api.sessions.delete(endingSession.id);
		} catch (caught) {
			// A session that is already gone (404) has already reached the state
			// this function is trying to produce.
			const alreadyEnded = caught instanceof ApiError && caught.status === 404;
			if (!alreadyEnded && active) {
				session = endingSession;
				frameLoading = true;
				error = caught instanceof ApiError ? caught.detail : 'Unable to end session.';
			}
		} finally {
			ending = false;
		}
	}

	function handlePageHide() {
		if (session) teardownSessionOnUnload(session.id);
	}

	// Reentrancy guard for the navigation interceptor below: once it has
	// cancelled and started cleanup for the current departure, the programmatic
	// `goto` it issues afterward must be let through rather than re-cancelled,
	// or the component would cancel its own replacement navigation forever.
	let leaving = false;

	beforeNavigate((navigation) => {
		// `navigation.to` is null for a navigation leaving the app entirely
		// (closing the tab, going back to a page outside it); those never reach
		// here as anything but a page unload, which `pagehide` covers instead —
		// cancelling with nothing to resume to would strand the user.
		if (!session || leaving || !navigation.to) return;
		leaving = true;
		navigation.cancel();
		const destination = navigation.to.url;
		void (async () => {
			await endSession();
			await goto(destination, { replaceState: navigation.type === 'popstate' });
		})();
	});

	$effect(() => {
		// Reading these makes the effect re-run the whole lifecycle — new
		// session, fresh listeners — when SvelteKit reuses this component for a
		// sibling notebook's run/edit route instead of remounting it.
		void notebookId;
		void mode;
		active = true;
		// A prior lifecycle's navigation has already fully settled by the time
		// this runs again (whether via a fresh mount or a reused instance
		// picking up a new notebook id) — the next navigation away is a new,
		// independent departure that needs its own interception.
		leaving = false;
		void startSession();
		window.addEventListener('pagehide', handlePageHide);
		return () => {
			active = false;
			window.removeEventListener('pagehide', handlePageHide);
			void endSession();
		};
	});
</script>

<svelte:head>
	<title>{heading} | MarimoHub</title>
</svelte:head>

<section class="flex min-h-0 flex-1 flex-col bg-app-bg">
	{#if loading}
		<div class="grid flex-1 place-items-center p-6 text-center" aria-live="polite">
			<div class="w-full max-w-sm rounded-lg border border-app-line bg-app-card p-8 shadow-[var(--shadow-sm)]">
				<LoaderCircle size={28} class="mx-auto animate-spin text-brand-strong" />
				<p class="mt-5 text-xs font-semibold uppercase tracking-[0.14em] text-brand-strong">Starting session</p>
				<p class="mt-2 text-sm text-app-muted">Marimo is spinning up. This can take a few seconds.</p>
			</div>
		</div>
	{:else if error}
		<div class="grid flex-1 place-items-center overflow-y-auto p-6">
			{#if errorKind === 'forbidden'}
				<div class="w-full max-w-md rounded-lg border border-app-line bg-app-card p-6 shadow-[var(--shadow-sm)]" role="alert">
					<span class="grid size-10 place-items-center rounded-md bg-app-warn-bg text-app-warn"><Lock size={18} /></span>
					<p class="mt-4 text-base font-semibold text-app-fg">Editor role required</p>
					<p class="mt-1 text-sm text-app-muted">{error}</p>
					<div class="mt-5">
						<Button intent="secondary" href={backHref}><ArrowLeft size={15} />{backLabel}</Button>
					</div>
				</div>
			{:else if errorKind === 'notfound'}
				<section class="w-full max-w-md rounded-lg border border-app-line bg-app-card p-8 text-center shadow-[var(--shadow-sm)]" role="alert">
					<p class="text-xs font-semibold uppercase tracking-[0.14em] text-app-muted">404</p>
					<h1 class="mt-2">Notebook unavailable</h1>
					<p class="mt-2 text-sm text-app-muted">{error}</p>
					<Button intent="primary" class="mt-6" href="/discover">Browse notebooks</Button>
				</section>
			{:else}
				<div class="w-full max-w-md rounded-lg border border-app-line bg-app-card p-6 shadow-[var(--shadow-sm)]" role="alert">
					<span class="grid size-10 place-items-center rounded-md bg-app-danger/10 text-app-danger"><CircleAlert size={18} /></span>
					<p class="mt-4 text-base font-semibold text-app-fg">Session failed</p>
					<p class="mt-1 text-sm text-app-muted">{error}</p>
					<div class="mt-5 flex flex-wrap gap-2">
						<Button intent="primary" type="button" onclick={() => void startSession()}><RotateCcw size={15} />Try again</Button>
						<Button intent="secondary" href={backHref}><ArrowLeft size={15} />{backLabel}</Button>
					</div>
				</div>
			{/if}
		</div>
	{:else if session}
		<div class="relative flex min-h-0 flex-1 overflow-hidden bg-app-card">
			{#if frameLoading}
				<div class="absolute inset-0 z-10 grid place-items-center bg-app-bg/95 p-6 text-center" aria-live="polite">
					<div>
						<LoaderCircle size={26} class="mx-auto animate-spin text-brand-strong" />
						<p class="mt-4 text-xs font-semibold uppercase tracking-[0.14em] text-brand-strong">Loading frame</p>
						<p class="mt-2 text-sm text-app-muted">Connecting to the marimo session.</p>
					</div>
				</div>
			{/if}
			<iframe class="h-full min-h-0 w-full flex-1 border-0 bg-app-card" src={iframeSrc} title={`${heading} marimo session`} onload={() => (frameLoading = false)} onerror={() => { frameLoading = false; error = 'The session frame could not be loaded.'; }}></iframe>
		</div>
	{:else}
		<div class="grid flex-1 place-items-center p-6 text-center">
			<div class="w-full max-w-sm rounded-lg border border-app-line bg-app-card p-8 shadow-[var(--shadow-sm)]">
				<p class="text-base font-semibold text-app-fg">Session ended</p>
				<p class="mt-1 text-sm text-app-muted">Start a new session to continue.</p>
				<Button intent="primary" class="mt-5" type="button" onclick={() => void startSession()}><Play size={15} />Start again</Button>
			</div>
		</div>
	{/if}
</section>
