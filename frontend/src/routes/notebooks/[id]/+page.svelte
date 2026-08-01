<script lang="ts">
	import { goto, invalidateAll } from '$app/navigation';
	import { browser } from '$app/environment';
	import { ApiError, api, type Deployment, type NotebookVisibility } from '$lib/api';
	import { auth } from '$lib/stores/auth';
	import { notebookCapability, notebookWorkspaceEntry } from '$lib/notebookCapability';
	import { VISIBILITY_EXPLANATIONS, VISIBILITY_LABELS, VISIBILITY_ORDER } from '$lib/notebookVisibility';
	import { activeDeployment as activeDeploymentOf, createDeploymentStatus, knownDeployment } from '$lib/stores/deploymentStatus';
	import { workspaces } from '$lib/stores/workspaces';
	import Button from '$lib/components/Button.svelte';
	import WorkspaceTargetPicker from '$lib/components/WorkspaceTargetPicker.svelte';

	const SLUG_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
	const SLUG_MESSAGE = 'Use lowercase letters, numbers, and hyphens. Start and end with a letter or number.';
	const POLL_INTERVAL_MS = 15_000;

	let { data } = $props();

	// Derived, not a locally-owned copy: every mutation below reloads `data`
	// through `invalidateAll()` so this page always reflects what the backend
	// actually persisted, the same convention the Workspace detail page uses.
	const notebook = $derived(data.notebook);

	let slug = $state('');
	// Seeded once from a plain local, not the reactive `notebook`/`data`
	// bindings — the effect below immediately replaces this with a fresh
	// instance keyed on the current notebook id, on mount and on every change.
	// svelte-ignore state_referenced_locally -- intentional one-time seed; the
	// effect below re-keys this on every notebook id change, including the
	// first one, so staleness here can never actually occur.
	const initialNotebookId = data.notebook.id;
	let deploymentStatus = $state(createDeploymentStatus(initialNotebookId));
	let documentVisible = $state(true);
	let deployLoading = $state(false);
	let deployError = $state<string | null>(null);

	let forkConfirming = $state(false);
	let forkWorkspaceId = $state('');
	let forkLoading = $state(false);
	let forkError = $state<string | null>(null);

	let visibilityBusy = $state(false);
	let visibilityError = $state<string | null>(null);
	let titleValue = $state('');
	let titleBusy = $state(false);
	let titleError = $state<string | null>(null);

	let confirmingDelete = $state(false);
	let deleteBusy = $state(false);
	let deleteError = $state<string | null>(null);

	const createdAt = $derived(formatDate(notebook.created_at));
	const updatedAt = $derived(formatDate(notebook.updated_at));
	const hasSource = $derived(Boolean(notebook.source && notebook.source.trim().length > 0));
	const isAuthenticated = $derived(Boolean($auth.token));

	const capability = $derived(notebookCapability($workspaces, notebook.workspace_id));
	const workspaceHydrating = $derived(capability === 'hydrating');
	// Backend role, not local auth state, is the write boundary: a signed-in
	// Viewer or a non-member reader must never see Edit/Visibility/Delete/Deploy.
	const mayWriteNotebook = $derived(capability === 'write');
	const workspaceEntry = $derived(notebookWorkspaceEntry($workspaces.items, notebook.workspace_id));

	const forkCountLabel = $derived(`${notebook.fork_count} ${notebook.fork_count === 1 ? 'fork' : 'forks'}`);
	const loginHref = $derived(`/auth/login?next=${encodeURIComponent(`/notebooks/${notebook.id}`)}`);
	const activeDeployment = $derived(activeDeploymentOf($deploymentStatus));
	const deployment = $derived(knownDeployment($deploymentStatus));
	const deploymentPending = $derived($deploymentStatus.status === 'loading' && $deploymentStatus.deployment === null);
	const deploymentError = $derived($deploymentStatus.status === 'error' ? $deploymentStatus.error : null);
	const publicDeploymentHref = $derived(activeDeployment ? publicDeploymentUrl(activeDeployment) : '');
	const slugInvalid = $derived(slug.trim().length > 0 && !SLUG_PATTERN.test(slug.trim()));

	// Reset and (re)load deployment state whenever the notebook changes —
	// covers first mount and SvelteKit reusing this component across two
	// different notebooks' detail pages without remounting it.
	$effect(() => {
		const status = createDeploymentStatus(notebook.id);
		deploymentStatus = status;
		void status.refresh();

		function handleVisibility() {
			documentVisible = document.visibilityState === 'visible';
			if (documentVisible) void status.refresh();
		}
		function handleFocus() {
			void status.refresh();
		}
		documentVisible = document.visibilityState === 'visible';
		document.addEventListener('visibilitychange', handleVisibility);
		window.addEventListener('focus', handleFocus);
		return () => {
			document.removeEventListener('visibilitychange', handleVisibility);
			window.removeEventListener('focus', handleFocus);
		};
	});

	$effect(() => {
		titleValue = notebook.title;
		titleError = null;
	});

	// Poll only while there is a running/sleeping deployment to observe and the
	// tab is actually visible — a stopped deployment or a backgrounded tab has
	// nothing worth refreshing on a timer.
	$effect(() => {
		if (!documentVisible || !activeDeployment) return;
		const timer = setInterval(() => void deploymentStatus.refresh(), POLL_INTERVAL_MS);
		return () => clearInterval(timer);
	});

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
			const result = await api.notebooks.deploy(notebook.id, { slug: slug.trim() || undefined });
			deploymentStatus.commit(result);
			slug = result.slug;
		} catch (caught) {
			deployError = caught instanceof ApiError ? caught.detail : 'Unable to deploy notebook.';
		} finally {
			deployLoading = false;
		}
	}

	async function stopDeployment() {
		if (!deployment) return;
		deployLoading = true;
		deployError = null;
		try {
			await api.deployments.delete(deployment.slug);
			deploymentStatus.commit({ ...deployment, status: 'stopped' });
		} catch (caught) {
			deployError = caught instanceof ApiError ? caught.detail : 'Unable to stop deployment.';
		} finally {
			deployLoading = false;
		}
	}

	function startFork() {
		forkConfirming = true;
		forkError = null;
	}

	function cancelFork() {
		forkConfirming = false;
		forkWorkspaceId = '';
		forkError = null;
	}

	async function confirmFork() {
		if (!forkWorkspaceId) return;
		forkLoading = true;
		forkError = null;
		try {
			const fork = await api.notebooks.fork(notebook.id, { workspace_id: forkWorkspaceId });
			await goto(`/notebooks/${fork.id}/edit`);
		} catch (caught) {
			forkError = caught instanceof ApiError ? caught.detail : 'Unable to fork notebook.';
		} finally {
			forkLoading = false;
		}
	}

	async function changeVisibility(next: NotebookVisibility) {
		if (next === notebook.visibility) return;
		visibilityBusy = true;
		visibilityError = null;
		try {
			await api.notebooks.publish(notebook.id, { visibility: next });
			await invalidateAll();
		} catch (caught) {
			visibilityError = caught instanceof ApiError ? caught.detail : 'Unable to update visibility.';
		} finally {
			visibilityBusy = false;
		}
	}

	async function updateTitle() {
		const title = titleValue.trim();
		if (!title) {
			titleError = 'Enter a Notebook title.';
			return;
		}
		if (title === notebook.title) return;

		titleBusy = true;
		titleError = null;
		try {
			await api.notebooks.update(notebook.id, { title });
			await invalidateAll();
		} catch (caught) {
			titleError = caught instanceof ApiError ? caught.detail : 'Unable to update the Notebook title.';
		} finally {
			titleBusy = false;
		}
	}

	async function deleteNotebook() {
		deleteBusy = true;
		deleteError = null;
		try {
			await api.notebooks.delete(notebook.id);
			await goto('/discover');
		} catch (caught) {
			// A capability check that was fine a moment ago can go stale (the
			// caller was demoted, or the workspace was archived, elsewhere) —
			// re-sync before presenting a permission error the UI already knows
			// might no longer be accurate.
			if (caught instanceof ApiError && (caught.status === 403 || caught.status === 404)) {
				await workspaces.refresh();
			}
			deleteError = caught instanceof ApiError ? caught.detail : 'Unable to delete notebook.';
			deleteBusy = false;
		}
	}
</script>

<svelte:head>
	<title>{notebook.title} | MarimoHub</title>
</svelte:head>

<article class="space-y-8">
	<Button intent="secondary" size="sm" class="w-fit" href="/discover">Back to discover</Button>

	<section class="grid gap-8 lg:grid-cols-[minmax(0,1fr)_22rem] lg:items-start">
		<div class="rounded-[2rem] border border-slate-900/10 bg-white/80 p-7 shadow-xl shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20 sm:p-9">
			<div class="flex flex-wrap items-center gap-3">
				<span
					class="rounded-full bg-hub-50 px-4 py-2 text-sm font-semibold capitalize text-hub-950 dark:bg-hub-400/10 dark:text-hub-200"
					data-testid="visibility-badge"
				>
					{notebook.visibility}
				</span>
				<span class="rounded-full bg-slate-100 px-4 py-2 text-sm font-semibold text-slate-700 dark:bg-white/10 dark:text-slate-200">
					{forkCountLabel}
				</span>
				<span class="rounded-full bg-slate-100 px-4 py-2 text-sm font-semibold text-slate-700 dark:bg-white/10 dark:text-slate-200">
					{hasSource ? 'Source available' : 'Source unavailable'}
				</span>
			</div>

			<h1 class="mt-6 text-4xl font-black tracking-tight text-slate-950 dark:text-white sm:text-6xl">{notebook.title}</h1>
			<p class="mt-5 max-w-3xl text-lg leading-8 text-slate-700 dark:text-slate-300">
				{notebook.description ?? 'This notebook has no description yet.'}
			</p>

			<div class="mt-7 flex flex-wrap gap-2">
				{#if notebook.tags.length > 0}
					{#each notebook.tags as tag}
						<span class="rounded-full bg-slate-100 px-3 py-1 text-xs font-semibold text-slate-700 dark:bg-white/10 dark:text-slate-200">{tag}</span>
					{/each}
				{:else}
					<span class="text-sm font-semibold text-slate-500 dark:text-slate-400">No tags</span>
				{/if}
			</div>

			<div class="mt-8 grid gap-4 border-t border-slate-900/10 pt-6 text-sm dark:border-white/10 sm:grid-cols-2">
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
					<Button intent="primary" href={`/notebooks/${notebook.id}/run`}>Run</Button>
					{#if mayWriteNotebook}
						<Button intent="secondary" href={`/notebooks/${notebook.id}/edit`}>Edit</Button>
					{/if}
					{#if !isAuthenticated}
						<Button intent="secondary" href={loginHref}>Sign in to fork</Button>
					{:else if !forkConfirming}
						<Button intent="secondary" type="button" onclick={startFork}>Fork</Button>
					{/if}
				</div>
				<p class="mt-4 text-sm leading-6 text-slate-600 dark:text-slate-300">
					Open this notebook in marimo, or fork it into a Private Notebook in a Workspace you can write to.
				</p>

				{#if forkConfirming}
					<div class="mt-5 grid gap-3 border-t border-slate-900/10 pt-5 dark:border-white/10">
						<WorkspaceTargetPicker bind:value={forkWorkspaceId} id="fork-target" label="Fork into" />
						<div class="flex flex-wrap gap-3">
							<Button intent="primary" size="sm" type="button" onclick={() => void confirmFork()} disabled={forkLoading || !forkWorkspaceId}>
								{forkLoading ? 'Forking...' : 'Confirm fork'}
							</Button>
							<Button intent="secondary" size="sm" type="button" onclick={cancelFork} disabled={forkLoading}>Cancel</Button>
						</div>
					</div>
				{/if}

				{#if forkError}
					<p class="mt-4 rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm font-semibold text-red-900 dark:border-red-400/20 dark:bg-red-500/10 dark:text-red-100" role="alert">
						{forkError}
					</p>
				{/if}
			</div>

			{#if activeDeployment}
				<div class="rounded-[2rem] border border-slate-900/10 bg-white/75 p-5 shadow-lg shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20">
					<div class="flex items-center justify-between gap-2">
						<p class="text-sm font-semibold uppercase tracking-[0.2em] text-hub-700 dark:text-hub-300">Deployment</p>
						<span class="rounded-full bg-hub-50 px-3 py-1 text-xs font-bold capitalize text-hub-950 dark:bg-hub-400/10 dark:text-hub-200" data-testid="deployment-status">{activeDeployment.status}</span>
					</div>
					<a class="mt-4 block break-all font-bold text-slate-950 underline decoration-slate-400 underline-offset-4 hover:decoration-hub-500 dark:text-white dark:decoration-slate-500" href={publicDeploymentHref} target="_blank" rel="noreferrer">{publicDeploymentHref}</a>
					<Button intent="primary" class="mt-4 w-full" href={publicDeploymentHref} target="_blank" rel="noreferrer">
						Open public deployment
					</Button>
					<p class="mt-4 text-xs leading-5 text-slate-500 dark:text-slate-400">
						This app is public to anyone with the link, even though this notebook's visibility is {notebook.visibility}.
					</p>
					{#if mayWriteNotebook}
						<Button intent="secondary" class="mt-3 w-full" type="button" onclick={() => void stopDeployment()} disabled={deployLoading}>
							{deployLoading ? 'Working...' : 'Stop deployment'}
						</Button>
						{#if deployError}
							<p class="mt-3 rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm font-semibold text-red-900 dark:border-red-400/20 dark:bg-red-500/10 dark:text-red-100" role="alert">{deployError}</p>
						{/if}
					{/if}
				</div>
			{/if}

			{#if workspaceHydrating}
				<div class="rounded-[2rem] border border-slate-900/10 bg-white/75 p-5 text-sm font-semibold text-slate-600 shadow-lg shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:text-slate-300 dark:shadow-black/20">
					Checking your access to this notebook's Workspace...
				</div>
			{:else if mayWriteNotebook}
				<div class="rounded-[2rem] border border-slate-900/10 bg-white/75 p-5 shadow-lg shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20">
					<p class="text-sm font-semibold uppercase tracking-[0.2em] text-hub-700 dark:text-hub-300">Notebook settings</p>
					<form class="mt-5 grid gap-3" onsubmit={(event) => { event.preventDefault(); void updateTitle(); }}>
						<label class="grid gap-2 text-sm font-semibold text-slate-700 dark:text-slate-200" for="notebook-title">
							Notebook title
							<input
								class="rounded-2xl border border-slate-300/80 bg-white/80 px-4 py-3 text-sm text-slate-950 outline-none transition focus:border-hub-400 focus:ring-4 focus:ring-hub-200/60 dark:border-white/15 dark:bg-slate-950/60 dark:text-white dark:focus:ring-hub-400/15"
								id="notebook-title"
								bind:value={titleValue}
								disabled={titleBusy}
								aria-invalid={Boolean(titleError)}
							/>
						</label>
						<Button intent="secondary" size="sm" type="submit" disabled={titleBusy || !titleValue.trim() || titleValue.trim() === notebook.title}>
							{titleBusy ? 'Saving...' : 'Save title'}
						</Button>
					</form>
					{#if titleError}
						<p class="mt-3 rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm font-semibold text-red-800 dark:border-red-400/20 dark:bg-red-500/10 dark:text-red-200" role="alert">{titleError}</p>
					{/if}

					<div class="mt-6 border-t border-slate-900/10 pt-5 dark:border-white/10">
						<label class="text-sm font-semibold text-slate-700 dark:text-slate-200" for="visibility">Visibility</label>
						<select
							class="mt-2 w-full rounded-2xl border border-slate-300/80 bg-white/80 px-4 py-3 text-sm text-slate-950 outline-none transition focus:border-hub-400 focus:ring-4 focus:ring-hub-200/60 dark:border-white/15 dark:bg-slate-950/60 dark:text-white"
							id="visibility"
							value={notebook.visibility}
							disabled={visibilityBusy}
							onchange={(event) => void changeVisibility(event.currentTarget.value as NotebookVisibility)}
						>
							{#each VISIBILITY_ORDER as option}
								<option value={option}>{VISIBILITY_LABELS[option]}</option>
							{/each}
						</select>
						<ul class="mt-3 space-y-1 text-xs leading-5 text-slate-500 dark:text-slate-400">
							{#each VISIBILITY_ORDER as option}
								<li><span class="font-semibold text-slate-700 dark:text-slate-200">{VISIBILITY_LABELS[option]}</span> — {VISIBILITY_EXPLANATIONS[option]}</li>
							{/each}
						</ul>
						{#if visibilityError}
							<p class="mt-3 rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm font-semibold text-red-800 dark:border-red-400/20 dark:bg-red-500/10 dark:text-red-200" role="alert">{visibilityError}</p>
						{/if}
					</div>
				</div>

				{#if !activeDeployment}
					<div class="rounded-[2rem] border border-slate-900/10 bg-white/75 p-5 shadow-lg shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20">
						<p class="text-sm font-semibold uppercase tracking-[0.2em] text-hub-700 dark:text-hub-300">Deployment</p>
						{#if deploymentPending}
							<p class="mt-4 text-sm font-semibold text-slate-600 dark:text-slate-300">Checking deployment status...</p>
						{:else}
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
								<p class="mt-4 rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm font-semibold text-red-900 dark:border-red-400/20 dark:bg-red-500/10 dark:text-red-100" role="alert">{deployError}</p>
							{/if}

							<p class="mt-5 rounded-3xl bg-slate-100/80 p-4 text-sm leading-6 text-slate-600 dark:bg-white/10 dark:text-slate-300">
								Deployments start asleep and wake on the first public visit. A Deployment's app is public
								regardless of this notebook's Visibility.
							</p>
						{/if}

						{#if deploymentError}
							<p class="mt-3 text-xs font-semibold text-amber-700 dark:text-amber-300" role="alert">{deploymentError} Showing the last known status.</p>
						{/if}
					</div>
				{/if}

				<div class="rounded-[2rem] border border-red-200 bg-red-50/60 p-5 dark:border-red-400/20 dark:bg-red-500/5">
					<p class="text-sm font-semibold uppercase tracking-[0.2em] text-red-800 dark:text-red-200">Delete notebook</p>
					<p class="mt-3 text-sm leading-6 text-red-900/80 dark:text-red-100/80">
						Permanently deletes this notebook and its source. This cannot be undone.
					</p>

					{#if confirmingDelete}
						<div class="mt-4 space-y-3">
							<p class="text-sm font-semibold text-red-900 dark:text-red-100">
								Delete "{notebook.title}"? This action is permanent and cannot be undone.
							</p>
							<div class="flex flex-wrap gap-3">
								<Button intent="primary" size="sm" type="button" onclick={() => void deleteNotebook()} disabled={deleteBusy}>
									{deleteBusy ? 'Deleting...' : 'Yes, delete this notebook'}
								</Button>
								<Button intent="secondary" size="sm" type="button" onclick={() => (confirmingDelete = false)} disabled={deleteBusy}>
									Cancel
								</Button>
							</div>
						</div>
					{:else}
						<Button intent="secondary" size="sm" class="mt-4" type="button" onclick={() => (confirmingDelete = true)}>
							Delete notebook
						</Button>
					{/if}

					{#if deleteError}
						<p class="mt-4 rounded-2xl border border-red-300 bg-red-100 px-4 py-3 text-sm font-semibold text-red-900 dark:border-red-400/30 dark:bg-red-500/10 dark:text-red-100" role="alert">{deleteError}</p>
					{/if}
				</div>
			{/if}

			<div class="rounded-[2rem] border border-slate-900/10 bg-white/75 p-5 shadow-lg shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20">
				<p class="text-sm font-semibold uppercase tracking-[0.2em] text-hub-700 dark:text-hub-300">Workspace</p>
				{#if workspaceEntry}
					<p class="mt-3 text-lg font-bold text-slate-950 dark:text-white">{workspaceEntry.name}</p>
					<p class="font-mono text-sm text-slate-500 dark:text-slate-400">{workspaceEntry.slug}</p>
				{:else if workspaceHydrating}
					<p class="mt-3 text-sm font-semibold text-slate-600 dark:text-slate-300">Loading...</p>
				{:else}
					<p class="mt-3 text-sm leading-6 text-slate-600 dark:text-slate-300">Only members of this notebook's Workspace can see its name.</p>
				{/if}
			</div>

			<div class="rounded-[2rem] border border-slate-900/10 bg-white/75 p-5 shadow-lg shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20">
				<p class="text-sm font-semibold uppercase tracking-[0.2em] text-hub-700 dark:text-hub-300">Lineage</p>
				{#if notebook.parent_id}
					{#if notebook.parent_title}
						<p class="mt-4 text-sm leading-6 text-slate-600 dark:text-slate-300">
							Forked from
							<a class="font-bold text-slate-950 underline decoration-slate-400 underline-offset-4 hover:decoration-hub-500 dark:text-white dark:decoration-slate-500" href={`/notebooks/${notebook.parent_id}`}>{notebook.parent_title}</a>{#if notebook.parent_workspace_slug} in {notebook.parent_workspace_slug}{/if}.
						</p>
					{:else}
						<p class="mt-4 text-sm leading-6 text-slate-600 dark:text-slate-300">Forked from a notebook that is unavailable to you.</p>
					{/if}
				{:else}
					<p class="mt-4 text-sm leading-6 text-slate-600 dark:text-slate-300">Original notebook.</p>
				{/if}
			</div>
		</aside>
	</section>
</article>
