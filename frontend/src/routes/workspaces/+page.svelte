<script lang="ts">
	import { page } from '$app/state';
	import { ApiError, api } from '$lib/api';
	import { activeWorkspace } from '$lib/stores/activeWorkspace';
	import { auth } from '$lib/stores/auth';
	import { workspaces } from '$lib/stores/workspaces';
	import Button from '$lib/components/Button.svelte';

	const SLUG_PATTERN = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/;
	const SLUG_MESSAGE = 'Use lowercase letters, numbers, and hyphens (up to 63 characters). Start and end with a letter or number.';

	const NOTICES: Record<string, string> = {
		'workspace-archived': 'The workspace was archived.',
		'workspace-unavailable': 'That workspace is no longer available to you.'
	};

	const notice = $derived(NOTICES[page.url.searchParams.get('notice') ?? ''] ?? null);

	let name = $state('');
	let slug = $state('');
	let formError = $state<string | null>(null);
	let submitting = $state(false);
	let copied = $state(false);

	const slugInvalid = $derived(slug.trim().length > 0 && (!SLUG_PATTERN.test(slug.trim()) || slug.trim().length > 63));

	function formatDate(value: string) {
		return new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric', year: 'numeric' }).format(new Date(value));
	}

	async function copyUserId() {
		if (!$auth.currentUser) return;
		await navigator.clipboard.writeText($auth.currentUser.id);
		copied = true;
		setTimeout(() => (copied = false), 2000);
	}

	async function createWorkspace() {
		formError = null;
		if (!name.trim()) {
			formError = 'Enter a workspace name.';
			return;
		}
		if (slugInvalid) {
			formError = SLUG_MESSAGE;
			return;
		}

		submitting = true;
		try {
			const workspace = await api.workspaces.create({ name: name.trim(), slug: slug.trim() || undefined });
			activeWorkspace.set(workspace.id);
			name = '';
			slug = '';
			await workspaces.refresh();
		} catch (caught) {
			formError = caught instanceof ApiError ? caught.detail : 'Unable to create workspace.';
		} finally {
			submitting = false;
		}
	}
</script>

<svelte:head>
	<title>Workspaces | MarimoHub</title>
</svelte:head>

<section class="space-y-8">
	<div class="flex flex-wrap items-center justify-between gap-4">
		<div>
			<h1 class="text-4xl font-black tracking-tight text-slate-950 dark:text-white sm:text-5xl">Workspaces</h1>
			<p class="mt-3 max-w-2xl text-lg leading-8 text-slate-700 dark:text-slate-300">
				Every notebook you create or collaborate on belongs to a workspace.
			</p>
		</div>
		<Button intent="secondary" size="sm" href="/workspaces/archived">Archived workspaces</Button>
	</div>

	{#if notice}
		<p class="rounded-[2rem] border border-hub-200 bg-hub-50 px-6 py-4 text-sm font-semibold text-hub-950 dark:border-hub-400/20 dark:bg-hub-400/10 dark:text-hub-100" role="status">{notice}</p>
	{/if}

	{#if $auth.currentUser}
		<div class="rounded-[2rem] border border-slate-900/10 bg-white/80 p-6 shadow-lg shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20">
			<p class="text-sm font-semibold uppercase tracking-[0.2em] text-hub-700 dark:text-hub-300">Your User ID</p>
			<p class="mt-3 max-w-2xl text-sm leading-6 text-slate-600 dark:text-slate-300">
				Share this ID with a Workspace Owner so they can add you as a member. MarimoHub has no user directory or lookup by
				username or email — the raw ID is the only way an Owner can find you.
			</p>
			<div class="mt-4 flex flex-wrap items-center gap-3">
				<code class="rounded-2xl bg-slate-100 px-4 py-3 text-sm font-bold text-slate-950 dark:bg-white/10 dark:text-white">{$auth.currentUser.id}</code>
				<Button intent="secondary" size="sm" type="button" onclick={() => void copyUserId()}>
					{copied ? 'Copied!' : 'Copy'}
				</Button>
			</div>
		</div>
	{/if}

	<div class="grid gap-8 lg:grid-cols-[minmax(0,1fr)_22rem] lg:items-start">
		<div class="space-y-4">
			{#if $workspaces.status === 'loading' && $workspaces.items.length === 0}
				<p class="rounded-[2rem] border border-slate-900/10 bg-white/70 p-6 text-sm font-semibold text-slate-600 dark:border-white/10 dark:bg-white/10 dark:text-slate-300">
					Loading workspaces...
				</p>
			{:else if $workspaces.status === 'error'}
				<div class="rounded-[2rem] border border-red-200 bg-red-50 p-6 dark:border-red-400/20 dark:bg-red-500/10" role="alert">
					<p class="font-semibold text-red-900 dark:text-red-100">{$workspaces.error}</p>
					<Button class="mt-4" intent="secondary" size="sm" type="button" onclick={() => void workspaces.refresh()}>Retry</Button>
				</div>
			{/if}

			{#if $workspaces.items.length === 0 && $workspaces.status !== 'loading' && $workspaces.status !== 'error'}
				<p class="rounded-[2rem] border border-slate-900/10 bg-white/70 p-6 text-sm font-semibold text-slate-600 dark:border-white/10 dark:bg-white/10 dark:text-slate-300">
					You don't belong to any workspace yet. Create one to start adding notebooks.
				</p>
			{:else}
				{#each $workspaces.items as workspace (workspace.id)}
					<a
						href={`/workspaces/${workspace.id}`}
						class="block rounded-[2rem] border border-slate-900/10 bg-white/80 p-6 shadow-lg shadow-slate-900/5 backdrop-blur transition hover:border-hub-400 dark:border-white/10 dark:bg-white/10 dark:shadow-black/20"
					>
						<div class="flex flex-wrap items-center justify-between gap-3">
							<h2 class="text-xl font-bold text-slate-950 dark:text-white">{workspace.name}</h2>
							<span class="rounded-full bg-hub-50 px-3 py-1 text-xs font-bold capitalize text-hub-950 dark:bg-hub-400/10 dark:text-hub-200">{workspace.role}</span>
						</div>
						<p class="mt-2 font-mono text-sm text-slate-500 dark:text-slate-400">{workspace.slug}</p>
						<p class="mt-3 text-sm text-slate-600 dark:text-slate-300">Created {formatDate(workspace.created_at)}</p>
					</a>
				{/each}
			{/if}
		</div>

		<div class="rounded-[2rem] border border-slate-900/10 bg-white/80 p-6 shadow-xl shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20 lg:sticky lg:top-6">
			<p class="text-sm font-semibold uppercase tracking-[0.2em] text-hub-700 dark:text-hub-300">Create a workspace</p>
			<form class="mt-5 grid gap-4" onsubmit={(event) => { event.preventDefault(); void createWorkspace(); }}>
				<label class="grid gap-2 text-sm font-semibold text-slate-700 dark:text-slate-200">
					Name
					<input
						class="rounded-2xl border border-slate-300/80 bg-white/80 px-4 py-3 text-sm text-slate-950 outline-none transition focus:border-hub-400 focus:ring-4 focus:ring-hub-200/60 dark:border-white/15 dark:bg-slate-950/60 dark:text-white dark:focus:ring-hub-400/15"
						bind:value={name}
						disabled={submitting}
						placeholder="Research Team"
					/>
				</label>
				<label class="grid gap-2 text-sm font-semibold text-slate-700 dark:text-slate-200">
					Slug (optional)
					<input
						class="rounded-2xl border border-slate-300/80 bg-white/80 px-4 py-3 font-mono text-sm text-slate-950 outline-none transition focus:border-hub-400 focus:ring-4 focus:ring-hub-200/60 dark:border-white/15 dark:bg-slate-950/60 dark:text-white dark:focus:ring-hub-400/15"
						bind:value={slug}
						disabled={submitting}
						placeholder="research-team"
						aria-invalid={slugInvalid}
					/>
					{#if slugInvalid}
						<span class="font-medium text-red-700 dark:text-red-300">{SLUG_MESSAGE}</span>
					{:else}
						<span class="text-xs font-normal text-slate-500 dark:text-slate-400">Left blank, a slug is generated from the name. The slug can never change later.</span>
					{/if}
				</label>

				{#if formError}
					<p class="rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm font-semibold text-red-800 dark:border-red-400/20 dark:bg-red-500/10 dark:text-red-200" role="alert">{formError}</p>
				{/if}

				<Button type="submit" disabled={submitting || slugInvalid}>
					{submitting ? 'Creating...' : 'Create workspace'}
				</Button>
			</form>
		</div>
	</div>
</section>
