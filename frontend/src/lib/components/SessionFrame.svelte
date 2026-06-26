<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { browser } from '$app/environment';
	import { ApiError, api, resolveApiUrl, type Session, type SessionMode } from '$lib/api';
	import Button from '$lib/components/Button.svelte';

	type Props = {
		notebookId: string;
		mode: SessionMode;
		heading: string;
		backHref: string;
		backLabel: string;
	};

	let { notebookId, mode, heading, backHref, backLabel }: Props = $props();
	let session = $state<Session | null>(null);
	let loading = $state(true);
	let frameLoading = $state(false);
	let error = $state<string | null>(null);
	let active = false;
	let ending = $state(false);
	let saving = false;
	let autosaveTimer: ReturnType<typeof setInterval> | null = null;

	const iframeSrc = $derived(session ? resolveApiUrl(session.proxy_url) : '');
	const actionLabel = $derived(mode === 'edit' ? 'edit' : 'run');
	const isEditMode = $derived(mode === 'edit');

	function stopAutosave() {
		if (autosaveTimer) clearInterval(autosaveTimer);
		autosaveTimer = null;
	}

	function startAutosave(sessionId: string) {
		if (!isEditMode) return;
		stopAutosave();
		autosaveTimer = setInterval(() => {
			void saveSession(sessionId);
		}, 5000);
	}

	function sleep(ms: number) {
		return new Promise((resolve) => setTimeout(resolve, ms));
	}

	async function saveSession(sessionId = session?.id) {
		if (!isEditMode || !sessionId || saving) return;
		saving = true;
		try {
			// Marimo owns the editor state inside the iframe. Give its autosave a
			// short chance to flush before MarimoHub copies the saved source.
			await sleep(1500);
			await api.sessions.save(sessionId);
		} catch {
			// Marimo's proxied save endpoint is the primary persistence path; this
			// periodic copy is best-effort fallback.
		} finally {
			saving = false;
		}
	}

	async function startSession() {
		loading = true;
		session = null;
		frameLoading = false;
		error = null;
		try {
			const created = await api.sessions.create({ notebook_id: notebookId, mode });
			if (!active) {
				await api.sessions.delete(created.id);
				return;
			}
			session = created;
			frameLoading = true;
			startAutosave(created.id);
		} catch (caught) {
			if (!active) return;
			error = caught instanceof ApiError ? caught.detail : `Unable to start ${actionLabel} session.`;
		} finally {
			if (active) loading = false;
		}
	}

	async function endSession() {
		if (!session || ending) return;
		ending = true;
		const endingSession = session;
		session = null;
		frameLoading = false;
		try {
			stopAutosave();
			await saveSession(endingSession.id);
			await api.sessions.delete(endingSession.id);
		} catch (caught) {
			if (active) {
				session = endingSession;
				frameLoading = true;
				startAutosave(endingSession.id);
				error = caught instanceof ApiError ? caught.detail : 'Unable to end session.';
			}
		} finally {
			ending = false;
		}
	}

	function handleBeforeUnload() {
		void endSession();
	}

	onMount(() => {
		active = true;
		void startSession();
		window.addEventListener('beforeunload', handleBeforeUnload);
	});

	onDestroy(() => {
		active = false;
		if (browser) window.removeEventListener('beforeunload', handleBeforeUnload);
		stopAutosave();
		void endSession();
	});
</script>

<svelte:head>
	<title>{heading} | MarimoHub</title>
</svelte:head>

	<section class="flex min-h-0 flex-1 flex-col bg-white dark:bg-slate-950">
	<div class="flex shrink-0 flex-wrap items-center justify-between gap-3 border-t border-slate-900/10 bg-white/90 px-4 py-3 shadow-sm shadow-slate-950/5 backdrop-blur dark:border-white/10 dark:bg-slate-950/90 dark:shadow-black/20 sm:px-6">
		<div class="flex min-w-0 items-center gap-3">
			<Button intent="secondary" size="sm" class="w-fit" href={backHref}>Back to dashboard</Button>
			<div class="min-w-0">
				<p class="truncate text-sm font-black text-slate-950 dark:text-white">{heading}</p>
				<p class="text-xs font-semibold capitalize text-hub-700 dark:text-hub-300">{actionLabel} mode</p>
			</div>
		</div>

		{#if session}
			<div class="flex flex-wrap items-center gap-3">
				{#if isEditMode}
					<div class="text-sm font-semibold text-slate-600 dark:text-slate-300" aria-live="polite">Autosaves automatically.</div>
				{/if}
				<Button intent="secondary" size="sm" type="button" onclick={() => void endSession()} disabled={ending}>
					{ending ? 'Ending...' : 'End session'}
				</Button>
			</div>
		{/if}
	</div>

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
		<div class="rounded-[2rem] border border-red-200 bg-red-50 p-6 text-red-900 shadow-xl shadow-red-950/5 dark:border-red-400/20 dark:bg-red-500/10 dark:text-red-100" role="alert">
			<p class="text-lg font-black">Session failed</p>
			<p class="mt-2 text-sm font-semibold">{error}</p>
			<div class="mt-5 flex flex-wrap gap-3">
				<Button intent="primary" type="button" onclick={() => void startSession()}>Try again</Button>
				<a class="rounded-full border border-red-300/80 px-5 py-3 text-sm font-bold text-red-900 dark:border-red-200/30 dark:text-red-100" href={backHref}>{backLabel}</a>
			</div>
		</div>
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
