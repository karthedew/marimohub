<script lang="ts">
	import { browser } from '$app/environment';
	import { goto } from '$app/navigation';
	import { ApiError, api, type Deployment } from '$lib/api';
	import { auth } from '$lib/stores/auth';
	import Button from '$lib/components/Button.svelte';

	const SLUG_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
	const SLUG_MESSAGE = 'Use lowercase letters, numbers, and hyphens. Start and end with a letter or number.';

	let { data } = $props();
	let slug = $state('');
	let deployment = $state<Deployment | null>(null);
	let deployLoading = $state(false);
	let deployError = $state<string | null>(null);
	let forkLoading = $state(false);
	let forkError = $state<string | null>(null);

	const ownerName = $derived($auth.currentUser?.id === data.notebook.user_id ? $auth.currentUser.username : data.notebook.user_id);
	const createdAt = $derived(formatDate(data.notebook.created_at));
	const updatedAt = $derived(formatDate(data.notebook.updated_at));
	const hasSource = $derived(Boolean(data.notebook.source && data.notebook.source.trim().length > 0));
	const isOwner = $derived($auth.currentUser?.id === data.notebook.user_id);
	const isAuthenticated = $derived(Boolean($auth.token));
	const forkCountLabel = $derived(`${data.notebook.fork_count} ${data.notebook.fork_count === 1 ? 'fork' : 'forks'}`);
	const loginHref = $derived(`/auth/login?next=${encodeURIComponent(`/notebooks/${data.notebook.id}`)}`);
	const publicDeploymentHref = $derived(deployment ? publicDeploymentUrl(deployment) : '');
	const stopSlug = $derived(deployment && deployment.status !== 'stopped' ? deployment.slug : slug.trim());
	const slugInvalid = $derived(slug.trim().length > 0 && !SLUG_PATTERN.test(slug.trim()));

	function formatDate(value: string) {
		return new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric', year: 'numeric' }).format(new Date(value));
	}

	function publicDeploymentUrl(value: Deployment) {
		const backendPath = parseDeploymentPath(value.url);
		const path = backendPath ?? `/deploy/${value.slug}`;
		return browser ? new URL(path, window.location.origin).toString() : path;
	}

	function parseDeploymentPath(value: string) {
		try {
			const url = new URL(value, 'http://localhost');
			const match = url.pathname.match(/^\/api\/deployments\/([^/]+)$/);
			return match ? `/deploy/${match[1]}` : null;
		} catch {
			return null;
		}
	}

	async function deployNotebook() {
		if (slugInvalid) return;
		deployLoading = true;
		deployError = null;
		try {
			deployment = await api.notebooks.deploy(data.notebook.id, { slug: slug.trim() || undefined });
			slug = deployment.slug;
		} catch (caught) {
			deployError = caught instanceof ApiError ? caught.detail : 'Unable to deploy notebook.';
		} finally {
			deployLoading = false;
		}
	}

	async function forkNotebook() {
		if (!isAuthenticated) {
			forkError = 'Sign in before forking this notebook.';
			return;
		}

		forkLoading = true;
		forkError = null;
		try {
			const fork = await api.notebooks.fork(data.notebook.id);
			await goto(`/notebooks/${fork.id}/edit`);
		} catch (caught) {
			forkError = caught instanceof ApiError ? caught.detail : 'Unable to fork notebook.';
		} finally {
			forkLoading = false;
		}
	}

	async function stopDeployment() {
		if (!stopSlug) return;
		deployLoading = true;
		deployError = null;
		const stopping = deployment;
		try {
			await api.deployments.delete(stopSlug);
			deployment = stopping ? { ...stopping, status: 'stopped' } : { slug: stopSlug, status: 'stopped', url: `/api/deployments/${stopSlug}` };
		} catch (caught) {
			deployError = caught instanceof ApiError ? caught.detail : 'Unable to stop deployment.';
		} finally {
			deployLoading = false;
		}
	}
</script>

<svelte:head>
	<title>{data.notebook.title} | MarimoHub</title>
</svelte:head>

<article class="space-y-8">
	<Button intent="secondary" size="sm" class="w-fit" href="/discover">Back to discover</Button>

	<section class="grid gap-8 lg:grid-cols-[minmax(0,1fr)_22rem] lg:items-start">
		<div class="rounded-[2rem] border border-slate-900/10 bg-white/80 p-7 shadow-xl shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20 sm:p-9">
			<div class="flex flex-wrap items-center gap-3">
				<span class="rounded-full bg-hub-50 px-4 py-2 text-sm font-semibold capitalize text-hub-950 dark:bg-hub-400/10 dark:text-hub-200">
					{data.notebook.visibility}
				</span>
				<span class="rounded-full bg-slate-100 px-4 py-2 text-sm font-semibold text-slate-700 dark:bg-white/10 dark:text-slate-200">
					{forkCountLabel}
				</span>
				<span class="rounded-full bg-slate-100 px-4 py-2 text-sm font-semibold text-slate-700 dark:bg-white/10 dark:text-slate-200">
					{hasSource ? 'Source available' : 'Source unavailable'}
				</span>
			</div>

			<h1 class="mt-6 text-4xl font-black tracking-tight text-slate-950 dark:text-white sm:text-6xl">{data.notebook.title}</h1>
			<p class="mt-5 max-w-3xl text-lg leading-8 text-slate-700 dark:text-slate-300">
				{data.notebook.description ?? 'This notebook has no description yet.'}
			</p>

			<div class="mt-7 flex flex-wrap gap-2">
				{#if data.notebook.tags.length > 0}
					{#each data.notebook.tags as tag}
						<span class="rounded-full bg-slate-100 px-3 py-1 text-xs font-semibold text-slate-700 dark:bg-white/10 dark:text-slate-200">{tag}</span>
					{/each}
				{:else}
					<span class="text-sm font-semibold text-slate-500 dark:text-slate-400">No tags</span>
				{/if}
			</div>

			<div class="mt-8 grid gap-4 border-t border-slate-900/10 pt-6 text-sm dark:border-white/10 sm:grid-cols-3">
				<div>
					<p class="font-semibold uppercase tracking-[0.18em] text-slate-500 dark:text-slate-400">Owner</p>
					<p class="mt-2 font-bold text-slate-950 dark:text-white">{ownerName}</p>
				</div>
				<div>
					<p class="font-semibold uppercase tracking-[0.18em] text-slate-500 dark:text-slate-400">Created</p>
					<p class="mt-2 font-bold text-slate-950 dark:text-white">{createdAt}</p>
				</div>
				<div>
					<p class="font-semibold uppercase tracking-[0.18em] text-slate-500 dark:text-slate-400">Updated</p>
					<p class="mt-2 font-bold text-slate-950 dark:text-white">{updatedAt}</p>
				</div>
			</div>
		</div>

		<aside class="space-y-4 lg:sticky lg:top-6">
			<div class="rounded-[2rem] border border-slate-900/10 bg-white/75 p-5 shadow-lg shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20">
				<p class="text-sm font-semibold uppercase tracking-[0.2em] text-hub-700 dark:text-hub-300">Actions</p>
				<div class="mt-5 grid gap-3">
					<Button intent="primary" href={`/notebooks/${data.notebook.id}/run`}>Run</Button>
					{#if isOwner}
						<Button intent="secondary" href={`/notebooks/${data.notebook.id}/edit`}>Edit</Button>
					{/if}
					{#if isAuthenticated}
						<Button intent="secondary" type="button" onclick={() => void forkNotebook()} disabled={forkLoading}>
							{forkLoading ? 'Forking...' : 'Fork'}
						</Button>
					{:else}
						<Button intent="secondary" href={loginHref}>Sign in to fork</Button>
					{/if}
				</div>
				<p class="mt-4 text-sm leading-6 text-slate-600 dark:text-slate-300">Open this notebook in marimo, or fork it into a private draft you can edit.</p>
				{#if forkError}
					<p class="mt-4 rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm font-semibold text-red-900 dark:border-red-400/20 dark:bg-red-500/10 dark:text-red-100">
						{forkError}
						{#if !isAuthenticated}
							<a class="ml-1 underline decoration-red-300 underline-offset-4 hover:decoration-red-600 dark:decoration-red-200/60" href={loginHref}>Sign in</a>
						{/if}
					</p>
				{/if}
			</div>

			{#if isOwner}
				<div class="rounded-[2rem] border border-slate-900/10 bg-white/75 p-5 shadow-lg shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20">
					<p class="text-sm font-semibold uppercase tracking-[0.2em] text-hub-700 dark:text-hub-300">Deployment</p>
					<form class="mt-5 grid gap-3" onsubmit={(event) => { event.preventDefault(); void deployNotebook(); }}>
						<label class="grid gap-2 text-sm font-semibold text-slate-700 dark:text-slate-200">
							Slug
							<input
								class="rounded-2xl border border-slate-300/80 bg-white/80 px-4 py-3 text-sm text-slate-950 outline-none transition focus:border-hub-400 focus:ring-4 focus:ring-hub-200/60 dark:border-white/15 dark:bg-slate-950/60 dark:text-white dark:focus:ring-hub-400/15"
								bind:value={slug}
								placeholder="Optional public slug"
								disabled={deployLoading}
								aria-invalid={slugInvalid}
							/>
							{#if slugInvalid}
								<span class="font-medium text-red-700 dark:text-red-300">{SLUG_MESSAGE}</span>
							{/if}
						</label>
						<Button intent="primary" type="submit" disabled={deployLoading || slugInvalid}>
							{deployLoading ? 'Working...' : deployment?.status === 'stopped' ? 'Deploy again' : 'Deploy'}
						</Button>
					</form>

					{#if deployError}
						<p class="mt-4 rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm font-semibold text-red-900 dark:border-red-400/20 dark:bg-red-500/10 dark:text-red-100">{deployError}</p>
					{/if}

					{#if deployment}
						<div class="mt-5 rounded-3xl bg-slate-100/80 p-4 text-sm dark:bg-white/10">
							<div class="flex flex-wrap items-center justify-between gap-2">
								<a class="break-all font-bold text-slate-950 underline decoration-slate-400 underline-offset-4 hover:decoration-hub-500 dark:text-white dark:decoration-slate-500" href={publicDeploymentHref} target="_blank" rel="noreferrer">{publicDeploymentHref}</a>
								<span class="rounded-full bg-white px-3 py-1 text-xs font-bold capitalize text-slate-700 dark:bg-slate-950/60 dark:text-slate-200">{deployment.status}</span>
							</div>
							<Button intent="primary" class="mt-4 w-full" href={publicDeploymentHref} target="_blank" rel="noreferrer">
								Open public deployment
							</Button>
							{#if deployment.status !== 'stopped'}
								<Button intent="secondary" class="mt-3 w-full" type="button" onclick={() => void stopDeployment()} disabled={deployLoading}>
									Stop deployment
								</Button>
							{/if}
						</div>
					{:else if slug.trim()}
						<Button intent="secondary" class="mt-5 w-full" type="button" onclick={() => void stopDeployment()} disabled={deployLoading}>
							Stop deployment for this slug
						</Button>
					{:else}
						<p class="mt-5 rounded-3xl bg-slate-100/80 p-4 text-sm leading-6 text-slate-600 dark:bg-white/10 dark:text-slate-300">
							Deployments start asleep and wake on the first public visit.
						</p>
					{/if}
				</div>
			{/if}

			<div class="rounded-[2rem] border border-slate-900/10 bg-white/75 p-5 shadow-lg shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20">
				<p class="text-sm font-semibold uppercase tracking-[0.2em] text-hub-700 dark:text-hub-300">Lineage</p>
				{#if data.notebook.parent_id}
					{#if data.notebook.parent_title}
						<p class="mt-4 text-sm leading-6 text-slate-600 dark:text-slate-300">
							Forked from
							<a class="font-bold text-slate-950 underline decoration-slate-400 underline-offset-4 hover:decoration-hub-500 dark:text-white dark:decoration-slate-500" href={`/notebooks/${data.notebook.parent_id}`}>{data.notebook.parent_title}</a>{#if data.notebook.parent_owner_username} by {data.notebook.parent_owner_username}{/if}.
						</p>
					{:else}
						<p class="mt-4 text-sm leading-6 text-slate-600 dark:text-slate-300">Forked from a notebook you cannot view.</p>
					{/if}
				{:else}
					<p class="mt-4 text-sm leading-6 text-slate-600 dark:text-slate-300">Original notebook.</p>
				{/if}
			</div>
		</aside>
	</section>
</article>
