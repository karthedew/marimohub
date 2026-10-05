<script lang="ts">
	import { goto, invalidateAll } from '$app/navigation';
	import { browser } from '$app/environment';
	import { ApiError, api, type Deployment, type NotebookVisibility } from '$lib/api';
	import { auth } from '$lib/stores/auth';
	import { notebookCapability, notebookWorkspaceEntry } from '$lib/notebookCapability';
	import { VISIBILITY_EXPLANATIONS, VISIBILITY_LABELS, VISIBILITY_ORDER } from '$lib/notebookVisibility';
	import { activeDeployment as activeDeploymentOf, createDeploymentStatus, knownDeployment } from '$lib/stores/deploymentStatus';
	import { workspaces } from '$lib/stores/workspaces';
	import { CalendarDays, Clock3, ExternalLink, FileCode, FileText, FolderKanban, GitBranch, GitFork, Pencil, Play, Rocket, Settings2, Square, Trash } from '@lucide/svelte';
	import Button from '$lib/components/Button.svelte';
	import { card, cardHeader, cardTitle, errorBanner, fieldError, fieldLabel, input, select, tagChip } from '$lib/design/classes';
	import Badge from '$lib/design/components/Badge.svelte';
	import Breadcrumbs from '$lib/design/components/Breadcrumbs.svelte';
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
	const visibilityTone = { public: 'ok', unlisted: 'warn', private: 'neutral' } as const;
	const deploymentTone = { running: 'ok', sleeping: 'warn', stopped: 'neutral' } as const;
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

<Breadcrumbs crumbs={[{ label: 'Discover', href: '/discover' }, { label: notebook.title, href: `/notebooks/${notebook.id}` }]} />

<div class="mb-6 flex flex-col justify-between gap-4 lg:flex-row lg:items-end">
	<div class="flex min-w-0 items-start gap-3">
		<span class="mt-1 hidden size-11 shrink-0 place-items-center rounded-lg bg-brand-soft text-brand-strong sm:grid"><FileText size={21} /></span>
		<div class="min-w-0">
			<div class="flex flex-wrap items-center gap-2 text-xs text-app-muted">
				<Badge tone={visibilityTone[notebook.visibility]} data-testid="visibility-badge">{notebook.visibility}</Badge>
				<span class="inline-flex items-center gap-1"><GitFork size={13} />{forkCountLabel}</span>
				<span aria-hidden="true">·</span>
				<span class="inline-flex items-center gap-1"><FileCode size={13} />{hasSource ? 'Source available' : 'Source unavailable'}</span>
			</div>
			<h1 class="mt-1.5 break-words">{notebook.title}</h1>
			<p class="mt-2 max-w-3xl text-sm leading-6 text-app-muted">
				{notebook.description ?? 'This notebook has no description yet.'}
			</p>
		</div>
	</div>
	<div class="flex shrink-0 flex-wrap items-center gap-2">
		<Button intent="primary" href={`/notebooks/${notebook.id}/run`}><Play size={15} />Run</Button>
		{#if mayWriteNotebook}
			<Button intent="secondary" href={`/notebooks/${notebook.id}/edit`}><Pencil size={14} />Edit</Button>
		{/if}
		{#if !isAuthenticated}
			<Button intent="secondary" href={loginHref}><GitFork size={14} />Sign in to fork</Button>
		{:else if !forkConfirming}
			<Button intent="secondary" type="button" onclick={startFork}><GitFork size={14} />Fork</Button>
		{/if}
	</div>
</div>

{#if forkConfirming}
	<section class="{card} mb-5 p-5" aria-labelledby="fork-heading">
		<h2 id="fork-heading" class={cardTitle}>Fork this notebook</h2>
		<p class="mt-1 text-sm text-app-muted">The fork starts as a Private Notebook in the Workspace you choose.</p>
		<div class="mt-4 flex flex-col gap-3 sm:flex-row sm:items-end">
			<div class="min-w-0 flex-1 sm:max-w-sm">
				<WorkspaceTargetPicker bind:value={forkWorkspaceId} id="fork-target" label="Fork into" />
			</div>
			<div class="flex flex-wrap gap-2">
				<Button intent="primary" type="button" class="h-10" onclick={() => void confirmFork()} disabled={forkLoading || !forkWorkspaceId}>
					<GitFork size={15} />{forkLoading ? 'Forking...' : 'Confirm fork'}
				</Button>
				<Button intent="secondary" type="button" class="h-10" onclick={cancelFork} disabled={forkLoading}>Cancel</Button>
			</div>
		</div>
	</section>
{/if}

{#if forkError}
	<p class="{errorBanner} mb-5" role="alert">{forkError}</p>
{/if}

<div class="grid gap-5 xl:grid-cols-[minmax(0,1fr)_360px] xl:items-start">
	<div class="grid min-w-0 gap-5">
		<section class={card} aria-labelledby="about-heading">
			<div class={cardHeader}>
				<h2 id="about-heading" class={cardTitle}>About</h2>
				<p class="mt-0.5 text-xs text-app-muted">Open this notebook in marimo, or fork it into a Private Notebook in a Workspace you can write to.</p>
			</div>
			<dl class="m-0 grid gap-0 divide-y divide-app-line text-sm">
				<div class="grid gap-1 px-5 py-3 sm:grid-cols-[9rem_minmax(0,1fr)] sm:items-center">
					<dt class="text-xs font-medium text-app-muted">Tags</dt>
					<dd class="m-0 flex flex-wrap gap-1.5">
						{#if notebook.tags.length > 0}
							{#each notebook.tags as tag (tag)}<span class={tagChip}>{tag}</span>{/each}
						{:else}
							<span class="text-app-muted">No tags</span>
						{/if}
					</dd>
				</div>
				<div class="grid gap-1 px-5 py-3 sm:grid-cols-[9rem_minmax(0,1fr)] sm:items-center">
					<dt class="text-xs font-medium text-app-muted">Access</dt>
					<dd class="m-0 text-app-fg">{VISIBILITY_LABELS[notebook.visibility]} — <span class="text-app-muted">{VISIBILITY_EXPLANATIONS[notebook.visibility]}</span></dd>
				</div>
				<div class="grid gap-1 px-5 py-3 sm:grid-cols-[9rem_minmax(0,1fr)] sm:items-center">
					<dt class="text-xs font-medium text-app-muted">Created</dt>
					<dd class="m-0 inline-flex items-center gap-1.5 font-medium"><CalendarDays size={14} class="text-app-muted" />{createdAt}</dd>
				</div>
				<div class="grid gap-1 px-5 py-3 sm:grid-cols-[9rem_minmax(0,1fr)] sm:items-center">
					<dt class="text-xs font-medium text-app-muted">Updated</dt>
					<dd class="m-0 inline-flex items-center gap-1.5 font-medium"><Clock3 size={14} class="text-app-muted" />{updatedAt}</dd>
				</div>
				<div class="grid gap-1 px-5 py-3 sm:grid-cols-[9rem_minmax(0,1fr)] sm:items-center">
					<dt class="text-xs font-medium text-app-muted">Lineage</dt>
					<dd class="m-0 inline-flex items-start gap-1.5">
						<GitBranch size={14} class="mt-0.5 shrink-0 text-app-muted" />
						{#if notebook.parent_id}
							{#if notebook.parent_title}
								<span>
									Forked from
									<a class="font-semibold text-brand-strong hover:underline" href={`/notebooks/${notebook.parent_id}`}>{notebook.parent_title}</a>{#if notebook.parent_workspace_slug}{' in '}<span class="font-mono text-xs">{notebook.parent_workspace_slug}</span>{/if}.
								</span>
							{:else}
								<span class="text-app-muted">Forked from a notebook that is unavailable to you.</span>
							{/if}
						{:else}
							<span class="text-app-muted">Original notebook.</span>
						{/if}
					</dd>
				</div>
			</dl>
		</section>

		{#if workspaceHydrating}
			<p class="{card} px-5 py-4 text-sm text-app-muted">Checking your access to this notebook's Workspace...</p>
		{:else if mayWriteNotebook}
			<section class={card} aria-labelledby="settings-heading">
				<div class="{cardHeader} flex items-center gap-3">
					<span class="grid size-9 place-items-center rounded-md bg-brand-soft text-brand-strong"><Settings2 size={18} /></span>
					<div>
						<h2 id="settings-heading" class={cardTitle}>Notebook settings</h2>
						<p class="mt-0.5 text-xs text-app-muted">Editors and Owners of this Workspace can change these.</p>
					</div>
				</div>
				<div class="grid gap-6 p-5 lg:grid-cols-2">
					<div>
						<form class="grid gap-1.5" onsubmit={(event) => { event.preventDefault(); void updateTitle(); }}>
							<label class={fieldLabel} for="notebook-title">Notebook title</label>
							<div class="flex gap-2">
								<input class={input} id="notebook-title" bind:value={titleValue} disabled={titleBusy} aria-invalid={Boolean(titleError)} />
								<Button intent="secondary" type="submit" class="h-10" disabled={titleBusy || !titleValue.trim() || titleValue.trim() === notebook.title}>
									{titleBusy ? 'Saving...' : 'Save title'}
								</Button>
							</div>
						</form>
						{#if titleError}
							<p class="{errorBanner} mt-3" role="alert">{titleError}</p>
						{/if}
					</div>

					<div class="grid gap-1.5 content-start">
						<label class={fieldLabel} for="visibility">Visibility</label>
						<select
							class={select}
							id="visibility"
							value={notebook.visibility}
							disabled={visibilityBusy}
							onchange={(event) => void changeVisibility(event.currentTarget.value as NotebookVisibility)}
						>
							{#each VISIBILITY_ORDER as option (option)}
								<option value={option}>{VISIBILITY_LABELS[option]}</option>
							{/each}
						</select>
						<ul class="m-0 mt-1 grid list-none gap-1 p-0 text-xs leading-5 text-app-muted">
							{#each VISIBILITY_ORDER as option (option)}
								<li><span class="font-semibold text-app-fg">{VISIBILITY_LABELS[option]}</span> — {VISIBILITY_EXPLANATIONS[option]}</li>
							{/each}
						</ul>
						{#if visibilityError}
							<p class="{errorBanner} mt-2" role="alert">{visibilityError}</p>
						{/if}
					</div>
				</div>
			</section>

			<section class="rounded-lg border border-app-danger/35 bg-app-card p-5" aria-labelledby="delete-heading">
				<div class="flex items-start gap-3">
					<span class="grid size-9 shrink-0 place-items-center rounded-md bg-app-danger/10 text-app-danger"><Trash size={17} /></span>
					<div class="min-w-0 flex-1">
						<h2 id="delete-heading" class={cardTitle}>Delete notebook</h2>
						<p class="mt-1 text-sm leading-6 text-app-muted">Permanently deletes this notebook and its source. This cannot be undone.</p>

						{#if confirmingDelete}
							<div class="mt-4 grid gap-3">
								<p class="text-sm font-semibold text-app-danger">
									Delete "{notebook.title}"? This action is permanent and cannot be undone.
								</p>
								<div class="flex flex-wrap gap-2">
									<Button intent="danger" type="button" onclick={() => void deleteNotebook()} disabled={deleteBusy}>
										{deleteBusy ? 'Deleting...' : 'Yes, delete this notebook'}
									</Button>
									<Button intent="secondary" type="button" onclick={() => (confirmingDelete = false)} disabled={deleteBusy}>Cancel</Button>
								</div>
							</div>
						{:else}
							<Button intent="secondary" class="mt-4" type="button" onclick={() => (confirmingDelete = true)}>Delete notebook</Button>
						{/if}

						{#if deleteError}
							<p class="{errorBanner} mt-4" role="alert">{deleteError}</p>
						{/if}
					</div>
				</div>
			</section>
		{/if}
	</div>

	<aside class="grid gap-5 xl:sticky xl:top-24" aria-label="Notebook context">
		{#if activeDeployment}
			<section class={card} aria-labelledby="deployment-heading">
				<div class="{cardHeader} flex items-center justify-between gap-2">
					<div class="flex items-center gap-2">
						<Rocket size={16} class="text-brand-strong" />
						<h2 id="deployment-heading" class={cardTitle}>Deployment</h2>
					</div>
					<Badge tone={deploymentTone[activeDeployment.status]} data-testid="deployment-status">{activeDeployment.status}</Badge>
				</div>
				<div class="grid gap-3 p-5">
					<a class="block break-all font-mono text-xs text-brand-strong hover:underline" href={publicDeploymentHref} target="_blank" rel="noreferrer">{publicDeploymentHref}</a>
					<Button intent="primary" class="w-full" href={publicDeploymentHref} target="_blank" rel="noreferrer">
						<ExternalLink size={15} />Open public deployment
					</Button>
					<p class="text-xs leading-5 text-app-muted">
						This app is public to anyone with the link, even though this notebook's visibility is {notebook.visibility}.
					</p>
					{#if mayWriteNotebook}
						<Button intent="secondary" class="w-full" type="button" onclick={() => void stopDeployment()} disabled={deployLoading}>
							{#if !deployLoading}<Square size={13} />{/if}{deployLoading ? 'Working...' : 'Stop deployment'}
						</Button>
						{#if deployError}
							<p class={errorBanner} role="alert">{deployError}</p>
						{/if}
					{/if}
				</div>
			</section>
		{:else if mayWriteNotebook && !workspaceHydrating}
			<section class={card} aria-labelledby="deployment-heading">
				<div class="{cardHeader} flex items-center gap-2">
					<Rocket size={16} class="text-brand-strong" />
					<h2 id="deployment-heading" class={cardTitle}>Deployment</h2>
				</div>
				<div class="p-5">
					{#if deploymentPending}
						<p class="text-sm text-app-muted">Checking deployment status...</p>
					{:else}
						<form class="grid gap-3" onsubmit={(event) => { event.preventDefault(); void deployNotebook(); }}>
							<div class="grid gap-1.5">
								<label class={fieldLabel} for="deploy-slug">Slug</label>
								<input
									class="{input} font-mono"
									id="deploy-slug"
									bind:value={slug}
									placeholder="Optional public slug"
									disabled={deployLoading}
									aria-invalid={slugInvalid}
								/>
								{#if slugInvalid}
									<p class={fieldError}>{SLUG_MESSAGE}</p>
								{/if}
							</div>
							<Button intent="primary" type="submit" disabled={deployLoading || slugInvalid}>
								{#if !deployLoading}<Rocket size={15} />{/if}{deployLoading ? 'Working...' : deployment?.status === 'stopped' ? 'Deploy again' : 'Deploy'}
							</Button>
						</form>

						{#if deployError}
							<p class="{errorBanner} mt-3" role="alert">{deployError}</p>
						{/if}

						<p class="mt-4 rounded-md bg-app-bg p-3 text-xs leading-5 text-app-muted">
							Deployments start asleep and wake on the first public visit. A Deployment's app is public
							regardless of this notebook's Visibility.
						</p>
					{/if}

					{#if deploymentError}
						<p class="mt-3 text-xs font-medium text-app-warn" role="alert">{deploymentError} Showing the last known status.</p>
					{/if}
				</div>
			</section>
		{/if}

		<section class="{card} p-5" aria-labelledby="workspace-heading">
			<div class="flex items-center gap-2">
				<FolderKanban size={16} class="text-brand-strong" />
				<h2 id="workspace-heading" class={cardTitle}>Workspace</h2>
			</div>
			{#if workspaceEntry}
				<p class="mt-3 text-sm font-semibold text-app-fg">{workspaceEntry.name}</p>
				<p class="font-mono text-xs text-app-muted">{workspaceEntry.slug}</p>
				<a class="mt-3 inline-block text-xs font-semibold text-brand-strong hover:underline" href={`/workspaces/${notebook.workspace_id}`}>Open workspace</a>
			{:else if workspaceHydrating}
				<p class="mt-3 text-sm text-app-muted">Loading...</p>
			{:else}
				<p class="mt-3 text-sm leading-6 text-app-muted">Only members of this notebook's Workspace can see its name.</p>
			{/if}
		</section>
	</aside>
</div>
