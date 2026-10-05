<script lang="ts">
	import {
		ArrowRight,
		Compass,
		FileText,
		FolderKanban,
		FolderPlus,
		GitFork,
		Globe,
		Play,
		Plus,
		Rocket,
		ShieldCheck,
		Sparkles
	} from '@lucide/svelte';
	import Button from '$lib/components/Button.svelte';
	import { VISIBILITY_EXPLANATIONS } from '$lib/notebookVisibility';
	import { errorBanner, iconTile, tagChip } from '$lib/design/classes';
	import Badge from '$lib/design/components/Badge.svelte';
	import EmptyState from '$lib/design/components/EmptyState.svelte';
	import PageHeader from '$lib/design/components/PageHeader.svelte';

	let { data } = $props();

	const ROLE_DETAIL = {
		owner: 'Administers members and archive',
		editor: 'Creates, edits, and deploys notebooks',
		viewer: 'Reads every notebook here'
	} as const;

	const visibilityTone = { public: 'ok', unlisted: 'warn', private: 'neutral' } as const;

	const publicCount = $derived(data.authenticated ? data.notebooks.items.filter((item) => item.visibility === 'public').length : 0);
	const unlistedCount = $derived(data.authenticated ? data.notebooks.items.filter((item) => item.visibility === 'unlisted').length : 0);

	const features = [
		{ icon: Compass, title: 'Discover', detail: 'Browse every Public notebook, plus everything in the Workspaces you belong to.' },
		{ icon: GitFork, title: 'Fork', detail: 'Copy any notebook you can read into a Workspace you can write to.' },
		{ icon: Play, title: 'Run', detail: 'Open a live marimo session in the browser, no local setup required.' },
		{ icon: Rocket, title: 'Deploy', detail: 'Publish a notebook as a public app that wakes on its first visit.' }
	];

	function formatDate(value: string) {
		return new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric' }).format(new Date(value));
	}
</script>

<svelte:head>
	<title>{data.authenticated ? 'Notebooks | MarimoHub' : 'MarimoHub'}</title>
</svelte:head>

{#if data.authenticated}
	<PageHeader title="Notebooks" eyebrow={data.workspace?.name ?? 'Your notebooks'} icon={FolderKanban}>
		{#snippet description()}
			{#if data.workspace}
				{data.notebooks.total} {data.notebooks.total === 1 ? 'notebook' : 'notebooks'} in this Workspace
			{:else}
				Create or join a Workspace to begin
			{/if}
		{/snippet}
		{#snippet actions()}
			{#if data.workspace && data.workspace.role !== 'viewer'}
				<Button intent="primary" href={`/notebooks/new?workspace=${data.workspace.id}`}><Plus size={16} strokeWidth={2.4} />New notebook</Button>
			{/if}
		{/snippet}
	</PageHeader>

	{#if data.error}
		<div class={errorBanner} role="alert">{data.error}</div>
	{:else if !data.workspace}
		<EmptyState
			titleAs="h2"
			icon={FolderPlus}
			title="Create your first Workspace"
			detail="Workspaces are where Notebooks live and where you manage who can access them."
		>
			<Button href="/workspaces?create=1"><Plus size={16} />Create workspace</Button>
		</EmptyState>
	{:else}
		<div class="mb-6 grid gap-3 sm:grid-cols-3">
			<div class="rounded-lg border border-app-line bg-app-card p-4">
				<div class="flex items-center justify-between">
					<span class="text-xs font-medium text-app-muted">In this Workspace</span>
					<FileText size={16} class="text-brand-strong" />
				</div>
				<p class="mt-2 text-2xl font-semibold tracking-tight">{data.notebooks.total}</p>
				<p class="mt-1 text-xs text-app-muted">{data.notebooks.total === 1 ? 'notebook' : 'notebooks'}</p>
			</div>
			<div class="rounded-lg border border-app-line bg-app-card p-4">
				<div class="flex items-center justify-between">
					<span class="text-xs font-medium text-app-muted">Shared beyond members</span>
					<Globe size={16} class="text-app-success" />
				</div>
				<p class="mt-2 text-2xl font-semibold tracking-tight">{publicCount + unlistedCount}</p>
				<p class="mt-1 text-xs text-app-muted">{publicCount} public · {unlistedCount} unlisted</p>
			</div>
			<div class="rounded-lg border border-app-line bg-app-card p-4">
				<div class="flex items-center justify-between">
					<span class="text-xs font-medium text-app-muted">Your role</span>
					<ShieldCheck size={16} class="text-app-muted" />
				</div>
				<p class="mt-2 text-2xl font-semibold capitalize tracking-tight">{data.workspace.role}</p>
				<p class="mt-1 text-xs text-app-muted">{ROLE_DETAIL[data.workspace.role]}</p>
			</div>
		</div>

		{#if data.notebooks.items.length === 0}
			<EmptyState
				icon={FileText}
				title="No notebooks yet"
				detail="Start from a blank marimo notebook, upload a Python file, or import from GitLab."
			>
				{#if data.workspace.role !== 'viewer'}
					<Button href={`/notebooks/new?workspace=${data.workspace.id}`}><Plus size={16} />Create notebook</Button>
				{/if}
			</EmptyState>
		{:else}
			<section aria-labelledby="recent-heading">
				<div class="mb-3 flex items-end justify-between gap-3">
					<div>
						<h2 id="recent-heading" class="m-0 border-0 p-0 text-base font-semibold">Recently updated</h2>
						<p class="mt-0.5 text-xs text-app-muted">Most recent first · {data.notebooks.items.length} shown</p>
					</div>
				</div>
				<div class="overflow-hidden rounded-lg border border-app-line bg-app-card">
					{#each data.notebooks.items as notebook (notebook.id)}
						<a
							class="group block border-b border-app-line px-4 py-3.5 no-underline last:border-0 hover:bg-app-sidebar-hover"
							href={`/notebooks/${notebook.id}`}
						>
							<div class="flex items-start gap-3">
								<span class="{iconTile} mt-0.5 size-9"><FileText size={17} /></span>
								<span class="min-w-0 flex-1">
									<span class="flex flex-wrap items-center gap-2">
										<span class="truncate text-sm font-semibold text-app-fg group-hover:text-brand-strong">{notebook.title}</span>
										<Badge tone={visibilityTone[notebook.visibility]} title={VISIBILITY_EXPLANATIONS[notebook.visibility]}>{notebook.visibility}</Badge>
									</span>
									<span class="mt-1 block truncate text-sm text-app-muted">{notebook.description ?? 'No description'}</span>
									<span class="mt-2 flex flex-wrap items-center gap-1.5">
										{#each notebook.tags as tag (tag)}<span class={tagChip}>{tag}</span>{/each}
										<span class="text-[11px] text-app-muted">Updated {formatDate(notebook.updated_at)} · {notebook.fork_count} {notebook.fork_count === 1 ? 'fork' : 'forks'}</span>
									</span>
								</span>
								<ArrowRight size={16} class="mt-2 shrink-0 text-app-muted transition group-hover:translate-x-0.5 group-hover:text-brand-strong" />
							</div>
						</a>
					{/each}
				</div>
			</section>
		{/if}
	{/if}
{:else}
	<section class="grid gap-8 py-2 lg:grid-cols-[minmax(0,1fr)_26rem] lg:items-center lg:py-8">
		<div>
			<div class="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.11em] text-app-muted">
				<Sparkles size={14} /> Marimo notebooks, ready to share
			</div>
			<h1 class="max-w-3xl text-[clamp(2rem,4.5vw,3.25rem)] leading-[1.1] tracking-[-0.04em]">Discover, fork, and run interactive notebooks.</h1>
			<p class="mt-4 max-w-2xl text-base leading-7 text-app-muted">
				MarimoHub is a collaborative home for marimo notebooks, from private research notebooks to published demos and deployed apps.
			</p>
			<div class="mt-7 flex flex-wrap gap-2">
				<Button intent="primary" size="lg" href="/discover">Browse notebooks<ArrowRight size={16} /></Button>
				<Button intent="secondary" size="lg" href="/notebooks/new">Create notebook</Button>
			</div>
		</div>

		<aside class="rounded-lg border border-app-line bg-app-card shadow-[var(--shadow-sm)]" aria-label="Notebook Visibility">
			<div class="flex items-center gap-3 border-b border-app-line p-5">
				<span class="grid size-9 place-items-center rounded-md bg-brand-soft text-brand-strong"><Globe size={18} /></span>
				<div>
					<p class="text-sm font-semibold">Open by default</p>
					<p class="mt-0.5 text-xs text-app-muted">Every Notebook has a Visibility.</p>
				</div>
			</div>
			<dl class="m-0 grid gap-3.5 p-5 text-xs">
				<div class="grid grid-cols-[5.5rem_minmax(0,1fr)] items-start gap-3">
					<dt><Badge tone="ok">public</Badge></dt>
					<dd class="m-0 leading-5 text-app-muted">Easy to find in Discover and readable by anyone.</dd>
				</div>
				<div class="grid grid-cols-[5.5rem_minmax(0,1fr)] items-start gap-3">
					<dt><Badge tone="warn">unlisted</Badge></dt>
					<dd class="m-0 leading-5 text-app-muted">Simple to share by direct link, hidden from discovery.</dd>
				</div>
				<div class="grid grid-cols-[5.5rem_minmax(0,1fr)] items-start gap-3">
					<dt><Badge>private</Badge></dt>
					<dd class="m-0 leading-5 text-app-muted">Stays inside its Workspace.</dd>
				</div>
			</dl>
		</aside>
	</section>

	<section class="mt-8 grid gap-3 sm:grid-cols-2 xl:grid-cols-4" aria-label="What you can do">
		{#each features as feature (feature.title)}
			{@const Icon = feature.icon}
			<div class="rounded-lg border border-app-line bg-app-card p-4">
				<span class="grid size-9 place-items-center rounded-md bg-brand-soft text-brand-strong"><Icon size={18} /></span>
				<h2 class="m-0 mt-3 border-0 p-0 text-sm font-semibold">{feature.title}</h2>
				<p class="mt-1 text-xs leading-5 text-app-muted">{feature.detail}</p>
			</div>
		{/each}
	</section>
{/if}
