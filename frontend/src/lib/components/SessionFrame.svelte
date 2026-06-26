<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { ApiError, api, resolveApiUrl, type Session, type SessionMode } from '$lib/api';

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

	const iframeSrc = $derived(session ? resolveApiUrl(session.proxy_url) : '');
	const actionLabel = $derived(mode === 'edit' ? 'edit' : 'run');

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
			await api.sessions.delete(endingSession.id);
		} catch (caught) {
			if (active) {
				session = endingSession;
				frameLoading = true;
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
		window.removeEventListener('beforeunload', handleBeforeUnload);
		void endSession();
	});
</script>

<svelte:head>
	<title>{heading} | MoLab</title>
</svelte:head>

	<section class="flex min-h-[calc(100vh-12rem)] flex-col gap-5">
	<div class="flex flex-wrap items-center justify-between gap-3">
		<div>
			<a class="inline-flex rounded-full bg-white/70 px-4 py-2 text-sm font-bold text-slate-700 shadow-sm hover:bg-white dark:bg-white/10 dark:text-slate-200 dark:hover:bg-white/15" href={backHref}>
				{backLabel}
			</a>
			<h1 class="mt-4 text-3xl font-black tracking-tight text-slate-950 dark:text-white sm:text-5xl">{heading}</h1>
		</div>

		{#if session}
			<button class="rounded-full border border-slate-300/80 bg-white/70 px-5 py-3 text-sm font-bold text-slate-800 shadow-sm hover:bg-white disabled:cursor-not-allowed disabled:opacity-60 dark:border-white/15 dark:bg-white/10 dark:text-slate-100 dark:hover:bg-white/15" type="button" onclick={() => void endSession()} disabled={ending}>
				{ending ? 'Ending...' : 'End session'}
			</button>
		{/if}
	</div>

	{#if loading}
		<div class="grid flex-1 place-items-center rounded-[2rem] border border-slate-900/10 bg-white/75 p-8 text-center shadow-xl shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20" aria-live="polite">
			<div>
				<div class="mx-auto h-3 w-44 overflow-hidden rounded-full bg-slate-200 dark:bg-white/10">
					<div class="h-full w-1/2 animate-pulse rounded-full bg-orange-500 dark:bg-orange-300"></div>
				</div>
				<p class="text-sm font-semibold uppercase tracking-[0.25em] text-orange-700 dark:text-orange-300">Starting session</p>
				<p class="mt-4 text-lg font-bold text-slate-950 dark:text-white">Marimo is spinning up. This can take a few seconds.</p>
			</div>
		</div>
	{:else if error}
		<div class="rounded-[2rem] border border-red-200 bg-red-50 p-6 text-red-900 shadow-xl shadow-red-950/5 dark:border-red-400/20 dark:bg-red-500/10 dark:text-red-100" role="alert">
			<p class="text-lg font-black">Session failed</p>
			<p class="mt-2 text-sm font-semibold">{error}</p>
			<div class="mt-5 flex flex-wrap gap-3">
				<button class="rounded-full bg-graphite px-5 py-3 text-sm font-bold text-white dark:bg-white dark:text-slate-950" type="button" onclick={() => void startSession()}>
					Try again
				</button>
				<a class="rounded-full border border-red-300/80 px-5 py-3 text-sm font-bold text-red-900 dark:border-red-200/30 dark:text-red-100" href={backHref}>{backLabel}</a>
			</div>
		</div>
	{:else if session}
		<div class="relative flex min-h-[32rem] flex-1 overflow-hidden rounded-[1.5rem] border border-slate-900/10 bg-white shadow-2xl shadow-slate-950/10 dark:border-white/10 dark:bg-slate-950 dark:shadow-black/30 sm:min-h-[42rem]">
			{#if frameLoading}
				<div class="absolute inset-0 z-10 grid place-items-center bg-white/95 p-8 text-center dark:bg-slate-950/95" aria-live="polite">
					<div>
						<div class="mx-auto h-3 w-44 overflow-hidden rounded-full bg-slate-200 dark:bg-white/10">
							<div class="h-full w-1/2 animate-pulse rounded-full bg-orange-500 dark:bg-orange-300"></div>
						</div>
						<p class="mt-5 text-sm font-semibold uppercase tracking-[0.25em] text-orange-700 dark:text-orange-300">Loading frame</p>
						<p class="mt-3 text-sm font-semibold text-slate-700 dark:text-slate-300">Connecting to the marimo session.</p>
					</div>
				</div>
			{/if}
			<iframe class="h-full min-h-[32rem] w-full flex-1 border-0 bg-white dark:bg-slate-950 sm:min-h-[42rem]" src={iframeSrc} title={`${heading} marimo session`} onload={() => (frameLoading = false)} onerror={() => { frameLoading = false; error = 'The session frame could not be loaded.'; }}></iframe>
		</div>
	{:else}
		<div class="grid flex-1 place-items-center rounded-[2rem] border border-slate-900/10 bg-white/75 p-8 text-center shadow-xl shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20">
			<div>
				<p class="text-lg font-black text-slate-950 dark:text-white">Session ended</p>
				<p class="mt-2 text-sm font-semibold text-slate-600 dark:text-slate-300">Start a new session to continue.</p>
				<button class="mt-5 rounded-full bg-graphite px-5 py-3 text-sm font-bold text-white dark:bg-white dark:text-slate-950" type="button" onclick={() => void startSession()}>
					Start again
				</button>
			</div>
		</div>
	{/if}
</section>
