<script lang="ts">
	import { page } from '$app/state';
	import { ApiError, api } from '$lib/api';
	import { activeWorkspace } from '$lib/stores/activeWorkspace';
	import { auth } from '$lib/stores/auth';
	import { workspaces } from '$lib/stores/workspaces';
	import { Archive, Check, ChevronRight, Copy, FolderKanban, FolderPlus, IdCard, Plus, RotateCcw } from '@lucide/svelte';
	import Button from '$lib/components/Button.svelte';
	import { card, cardHeader, cardTitle, errorBanner, fieldError, fieldHint, fieldLabel, input, noticeBanner } from '$lib/design/classes';
	import Badge from '$lib/design/components/Badge.svelte';
	import EmptyState from '$lib/design/components/EmptyState.svelte';
	import PageHeader from '$lib/design/components/PageHeader.svelte';

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

<PageHeader title="Workspaces" eyebrow="Collaboration" icon={FolderKanban} description="Every notebook you create or collaborate on belongs to a workspace.">
	{#snippet actions()}
		<Button intent="secondary" href="/workspaces/archived"><Archive size={15} />Archived workspaces</Button>
	{/snippet}
</PageHeader>

{#if notice}
	<p class="{noticeBanner} mb-5 flex items-center gap-2" role="status"><Check size={16} class="shrink-0 text-app-success" />{notice}</p>
{/if}

<div class="grid gap-5 xl:grid-cols-[minmax(0,1fr)_340px] xl:items-start">
	<div class="min-w-0">
		{#if $workspaces.status === 'loading' && $workspaces.items.length === 0}
			<p class="{card} px-4 py-10 text-center text-sm text-app-muted">Loading workspaces...</p>
		{:else if $workspaces.status === 'error'}
			<div class="{errorBanner} mb-4" role="alert">
				<p class="font-semibold">{$workspaces.error}</p>
				<Button class="mt-3" intent="secondary" size="sm" type="button" onclick={() => void workspaces.refresh()}><RotateCcw size={14} />Retry</Button>
			</div>
		{/if}

		{#if $workspaces.items.length === 0 && $workspaces.status !== 'loading' && $workspaces.status !== 'error'}
			<EmptyState icon={FolderPlus} title="No workspaces yet" detail="You don't belong to any workspace yet. Create one to start adding notebooks." />
		{:else if $workspaces.items.length > 0}
			<div class="overflow-hidden {card}">
				<div class="hidden grid-cols-[minmax(0,1fr)_110px_130px_24px] gap-3 border-b border-app-line bg-app-bg px-4 py-2.5 text-[10px] font-semibold uppercase tracking-[0.09em] text-app-muted md:grid">
					<span>Workspace</span><span>Role</span><span>Created</span><span></span>
				</div>
				{#each $workspaces.items as workspace (workspace.id)}
					<a
						href={`/workspaces/${workspace.id}`}
						class="group grid gap-2 border-b border-app-line px-4 py-3 text-app-fg no-underline last:border-0 hover:bg-app-sidebar-hover md:grid-cols-[minmax(0,1fr)_110px_130px_24px] md:items-center md:gap-3"
					>
						<span class="flex min-w-0 items-center gap-3">
							<span class="grid size-9 shrink-0 place-items-center rounded-md border border-app-line bg-app-bg text-xs font-bold text-app-muted">{workspace.name.slice(0, 2).toUpperCase()}</span>
							<span class="min-w-0">
								<span class="block truncate text-sm font-semibold group-hover:text-brand-strong">{workspace.name}</span>
								<span class="block truncate font-mono text-xs text-app-muted">{workspace.slug}</span>
							</span>
						</span>
						<span><Badge tone={workspace.role === 'owner' ? 'accent' : 'neutral'}>{workspace.role}</Badge></span>
						<span class="text-xs text-app-muted">Created {formatDate(workspace.created_at)}</span>
						<ChevronRight size={16} class="hidden text-app-muted group-hover:text-brand-strong md:block" />
					</a>
				{/each}
			</div>
		{/if}
	</div>

	<div class="grid gap-5 xl:sticky xl:top-24">
		<section class={card} aria-labelledby="create-workspace-heading">
			<div class={cardHeader}>
				<div class="flex items-center gap-3">
					<span class="grid size-9 place-items-center rounded-md bg-brand-soft text-brand-strong"><FolderPlus size={18} /></span>
					<div>
						<h2 id="create-workspace-heading" class={cardTitle}>Create a workspace</h2>
						<p class="mt-0.5 text-xs text-app-muted">You become its Owner.</p>
					</div>
				</div>
			</div>
			<form class="grid gap-4 p-5" onsubmit={(event) => { event.preventDefault(); void createWorkspace(); }}>
				<div class="grid gap-1.5">
					<label class={fieldLabel} for="workspace-name">Name</label>
					<input class={input} id="workspace-name" bind:value={name} disabled={submitting} placeholder="Research Team" />
				</div>
				<div class="grid gap-1.5">
					<label class={fieldLabel} for="workspace-slug">Slug (optional)</label>
					<input
						class="{input} font-mono"
						id="workspace-slug"
						bind:value={slug}
						disabled={submitting}
						placeholder="research-team"
						aria-invalid={slugInvalid}
						aria-describedby="workspace-slug-help"
					/>
					{#if slugInvalid}
						<p class={fieldError} id="workspace-slug-help">{SLUG_MESSAGE}</p>
					{:else}
						<p class={fieldHint} id="workspace-slug-help">Left blank, a slug is generated from the name. The slug can never change later.</p>
					{/if}
				</div>

				{#if formError}
					<p class={errorBanner} role="alert">{formError}</p>
				{/if}

				<Button type="submit" disabled={submitting || slugInvalid}>
					{#if !submitting}<Plus size={16} />{/if}{submitting ? 'Creating...' : 'Create workspace'}
				</Button>
			</form>
		</section>

		{#if $auth.currentUser}
			<section class="{card} p-5" aria-labelledby="user-id-heading">
				<div class="flex items-center gap-2">
					<IdCard size={16} class="text-brand-strong" />
					<h2 id="user-id-heading" class="m-0 border-0 p-0 text-sm font-semibold">Your User ID</h2>
				</div>
				<p class="mt-2 text-xs leading-5 text-app-muted">
					Workspace Owners adding members can find you by your name or username, or by your exact email address.
					Sharing this ID with an Owner works too.
				</p>
				<div class="mt-3 flex items-center gap-2">
					<code class="min-w-0 flex-1 truncate rounded-md border border-app-line bg-app-bg px-2.5 py-1.5 text-xs text-app-fg">{$auth.currentUser.id}</code>
					<Button intent="secondary" size="sm" type="button" onclick={() => void copyUserId()}>
						{#if copied}<Check size={14} />{:else}<Copy size={14} />{/if}{copied ? 'Copied!' : 'Copy'}
					</Button>
				</div>
			</section>
		{/if}
	</div>
</div>
