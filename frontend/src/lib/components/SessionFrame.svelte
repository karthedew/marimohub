<script lang="ts">
	import { beforeNavigate, goto } from '$app/navigation';
	import { page } from '$app/state';
	import { ApiError, api, resolveApiUrl, type Session, type SessionMode } from '$lib/api';
	import { teardownSessionOnUnload } from '$lib/sessionTeardown';
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

<section class="flex min-h-0 flex-1 flex-col bg-white dark:bg-slate-950">
	{#if loading}
		<div class="grid flex-1 place-items-center bg-white/75 p-8 text-center backdrop-blur dark:bg-slate-950" aria-live="polite">
			<div>
				<div class="mx-auto h-3 w-44 overflow-hidden rounded-full bg-slate-200 dark:bg-white/10">
					<div class="h-full w-1/2 animate-pulse rounded-full bg-hub-600 dark:bg-hub-300"></div>
				</div>
				<p class="text-sm font-semibold uppercase tracking-[0.25em] text-hub-700 dark:text-hub-300">Starting session</p>
				<p class="mt-4 text-lg font-bold text-slate-950 dark:text-white">Marimo is spinning up. This can take a few seconds.</p>
			</div>
		</div>
	{:else if error}
		{#if errorKind === 'forbidden'}
			<div class="rounded-[2rem] border border-red-200 bg-red-50 p-6 text-red-900 shadow-xl shadow-red-950/5 dark:border-red-400/20 dark:bg-red-500/10 dark:text-red-100" role="alert">
				<p class="text-lg font-black">Editor role required</p>
				<p class="mt-2 text-sm font-semibold">{error}</p>
				<div class="mt-5">
					<a class="rounded-full border border-red-300/80 px-5 py-3 text-sm font-bold text-red-900 dark:border-red-200/30 dark:text-red-100" href={backHref}>{backLabel}</a>
				</div>
			</div>
		{:else if errorKind === 'notfound'}
			<section class="mx-auto max-w-2xl rounded-[2rem] border border-slate-900/10 bg-white/80 p-8 text-center shadow-xl shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20" role="alert">
				<p class="mx-auto w-fit rounded-full bg-hub-50 px-4 py-2 text-sm font-semibold text-hub-950 dark:bg-hub-400/10 dark:text-hub-200">404</p>
				<h1 class="mt-5 text-4xl font-black tracking-tight text-slate-950 dark:text-white">Notebook unavailable</h1>
				<p class="mt-4 text-slate-700 dark:text-slate-300">{error}</p>
				<Button intent="primary" class="mt-7 w-fit" href="/discover">Browse notebooks</Button>
			</section>
		{:else}
			<div class="rounded-[2rem] border border-red-200 bg-red-50 p-6 text-red-900 shadow-xl shadow-red-950/5 dark:border-red-400/20 dark:bg-red-500/10 dark:text-red-100" role="alert">
				<p class="text-lg font-black">Session failed</p>
				<p class="mt-2 text-sm font-semibold">{error}</p>
				<div class="mt-5 flex flex-wrap gap-3">
					<Button intent="primary" type="button" onclick={() => void startSession()}>Try again</Button>
					<a class="rounded-full border border-red-300/80 px-5 py-3 text-sm font-bold text-red-900 dark:border-red-200/30 dark:text-red-100" href={backHref}>{backLabel}</a>
				</div>
			</div>
		{/if}
	{:else if session}
		<div class="relative flex min-h-0 flex-1 overflow-hidden bg-white dark:bg-slate-950">
			{#if frameLoading}
				<div class="absolute inset-0 z-10 grid place-items-center bg-white/95 p-8 text-center dark:bg-slate-950/95" aria-live="polite">
					<div>
						<div class="mx-auto h-3 w-44 overflow-hidden rounded-full bg-slate-200 dark:bg-white/10">
							<div class="h-full w-1/2 animate-pulse rounded-full bg-hub-600 dark:bg-hub-300"></div>
						</div>
						<p class="mt-5 text-sm font-semibold uppercase tracking-[0.25em] text-hub-700 dark:text-hub-300">Loading frame</p>
						<p class="mt-3 text-sm font-semibold text-slate-700 dark:text-slate-300">Connecting to the marimo session.</p>
					</div>
				</div>
			{/if}
			<iframe class="h-full min-h-0 w-full flex-1 border-0 bg-white dark:bg-slate-950" src={iframeSrc} title={`${heading} marimo session`} onload={() => (frameLoading = false)} onerror={() => { frameLoading = false; error = 'The session frame could not be loaded.'; }}></iframe>
		</div>
	{:else}
		<div class="grid flex-1 place-items-center bg-white/75 p-8 text-center backdrop-blur dark:bg-slate-950">
			<div>
				<p class="text-lg font-black text-slate-950 dark:text-white">Session ended</p>
				<p class="mt-2 text-sm font-semibold text-slate-600 dark:text-slate-300">Start a new session to continue.</p>
				<Button intent="primary" class="mt-5" type="button" onclick={() => void startSession()}>Start again</Button>
			</div>
		</div>
	{/if}
</section>
