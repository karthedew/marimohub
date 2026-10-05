<script lang="ts">
	import { ArrowRight, ChevronLeft, ChevronRight, Compass, FileText, GitFork, RotateCcw, Search, SearchX, Sparkles, Tag } from '@lucide/svelte';
	import { navigating } from '$app/state';
	import Button from '$lib/components/Button.svelte';
	import { VISIBILITY_EXPLANATIONS } from '$lib/notebookVisibility';
	import { errorBanner, iconTile, tagChip } from '$lib/design/classes';
	import Badge from '$lib/design/components/Badge.svelte';
	import EmptyState from '$lib/design/components/EmptyState.svelte';
	import PageHeader from '$lib/design/components/PageHeader.svelte';

	let { data } = $props();
	const loading = $derived(navigating.to !== null);

	const totalPages = $derived(Math.max(1, Math.ceil(data.notebooks.total / data.notebooks.page_size)));
	const showingFrom = $derived(data.notebooks.total === 0 ? 0 : (data.notebooks.page - 1) * data.notebooks.page_size + 1);
	const showingTo = $derived(Math.min(data.notebooks.page * data.notebooks.page_size, data.notebooks.total));
	const filtered = $derived(Boolean(data.filters.q || data.filters.tags));

	const visibilityTone = { public: 'ok', unlisted: 'warn', private: 'neutral' } as const;

	function pageHref(page: number) {
		const params = new URLSearchParams();
		if (data.filters.q) params.set('q', data.filters.q);
		if (data.filters.tags) params.set('tags', data.filters.tags);
		if (data.filters.semantic) params.set('semantic', '1');
		if (page > 1) params.set('page', String(page));
		const query = params.toString();
		return query ? `/discover?${query}` : '/discover';
	}
</script>

<svelte:head>
	<title>Discover | MarimoHub</title>
</svelte:head>

<PageHeader
	title="Browse notebooks"
	eyebrow="Discover"
	icon={Compass}
	description="Discover shows every Public Notebook, plus every Notebook in a Workspace you belong to, whatever its Visibility. An Unlisted Notebook outside your Workspaces stays reachable only by direct link."
/>

<form class="mb-6 rounded-lg border border-app-line bg-app-card p-2 shadow-[var(--shadow-sm)]" method="GET" role="search">
	<div class="flex items-center gap-2">
		<label class="relative block min-w-0 flex-1" for="q">
			<span class="visually-hidden">Search</span>
			<Search size={19} class="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-app-muted" />
			<input
				class="h-11 w-full rounded-md border-0 bg-transparent pl-10 pr-3 text-[15px] text-app-fg outline-none placeholder:text-app-muted"
				id="q"
				name="q"
				type="search"
				value={data.filters.q}
				placeholder="Signals, dashboards, optimization"
			/>
		</label>
		<Button intent="ink" type="submit" disabled={loading}>
			{loading ? 'Searching...' : 'Search'}
		</Button>
	</div>

	<div class="mt-2 flex flex-col gap-2 border-t border-app-line px-1 pt-2 sm:flex-row sm:items-center sm:gap-4">
		<div class="flex min-w-0 items-center gap-2 sm:w-80">
			<label class="flex shrink-0 items-center gap-1.5 text-xs font-semibold text-app-muted" for="tags"><Tag size={14} />Tags</label>
			<input
				class="h-8 w-full min-w-0 rounded-md border border-app-line bg-app-bg px-2.5 text-xs text-app-fg outline-none transition placeholder:text-app-muted focus:border-brand-strong"
				id="tags"
				name="tags"
				type="text"
				value={data.filters.tags}
				placeholder="grafana, iot"
			/>
		</div>
		<label class="flex w-fit items-center gap-2 text-xs font-medium text-app-muted">
			<input class="size-3.5 accent-[var(--brand-strong)]" type="checkbox" name="semantic" value="1" checked={data.filters.semantic} />
			<Sparkles size={13} />Semantic search
		</label>
	</div>
</form>

<section aria-labelledby="results-heading">
	<div class="mb-3 flex flex-wrap items-end justify-between gap-3">
		<div>
			<h2 id="results-heading" class="m-0 border-0 p-0 text-base font-semibold">
				{data.filters.q ? `Results for “${data.filters.q}”` : filtered ? 'Tagged results' : 'All visible'}
			</h2>
			<p class="mt-0.5 text-xs text-app-muted">
				{#if data.error}
					Results unavailable
				{:else}
					{data.notebooks.total} {data.notebooks.total === 1 ? 'notebook' : 'notebooks'}{data.filters.tags ? ` · tagged ${data.filters.tags}` : ''}{data.filters.semantic ? ' · semantic' : ''}
				{/if}
			</p>
		</div>
	</div>

	{#if data.error}
		<div class={errorBanner} role="alert">
			<p class="font-semibold">Unable to load notebooks</p>
			<p class="mt-1">{data.error}</p>
			<Button intent="secondary" size="sm" class="mt-3" href={pageHref(data.notebooks.page)}><RotateCcw size={14} />Try again</Button>
		</div>
	{:else if loading}
		<div class="overflow-hidden rounded-lg border border-app-line bg-app-card" aria-label="Loading notebooks">
			{#each Array(4) as _, index (index)}
				<div class="flex items-start gap-3 border-b border-app-line px-4 py-4 last:border-0">
					<div class="size-9 shrink-0 animate-pulse rounded-md bg-app-sidebar-hover"></div>
					<div class="flex-1 space-y-2">
						<div class="h-3.5 w-1/3 animate-pulse rounded bg-app-sidebar-hover"></div>
						<div class="h-3 w-2/3 animate-pulse rounded bg-app-sidebar-hover"></div>
					</div>
				</div>
			{/each}
		</div>
	{:else if data.notebooks.items.length === 0}
		<EmptyState icon={SearchX} title="No notebooks found" detail="Try a broader search or remove tag filters.">
			{#if filtered}<Button intent="secondary" size="sm" href="/discover">Clear filters</Button>{/if}
		</EmptyState>
	{:else}
		<div class="overflow-hidden rounded-lg border border-app-line bg-app-card">
			{#each data.notebooks.items as notebook (notebook.id)}
				<a class="group block border-b border-app-line px-4 py-4 no-underline last:border-0 hover:bg-app-sidebar-hover" href={`/notebooks/${notebook.id}`}>
					<div class="flex items-start gap-3">
						<span class="{iconTile} mt-0.5 size-9"><FileText size={17} /></span>
						<div class="min-w-0 flex-1">
							<div class="flex flex-wrap items-center gap-2">
								<h3 class="m-0 text-sm font-semibold text-app-fg group-hover:text-brand-strong">{notebook.title}</h3>
								<Badge tone={visibilityTone[notebook.visibility]} title={VISIBILITY_EXPLANATIONS[notebook.visibility]}>{notebook.visibility}</Badge>
							</div>
							<p class="mt-1 line-clamp-2 max-w-3xl text-sm text-app-muted">{notebook.description ?? 'No description provided.'}</p>
							<div class="mt-2 flex flex-wrap items-center gap-1.5">
								{#if notebook.tags.length > 0}
									{#each notebook.tags as tag (tag)}<span class={tagChip}>{tag}</span>{/each}
								{:else}
									<span class="text-[11px] text-app-muted">No tags</span>
								{/if}
								<span class="ml-1 inline-flex items-center gap-1 text-[11px] text-app-muted"><GitFork size={12} />{notebook.fork_count} {notebook.fork_count === 1 ? 'fork' : 'forks'}</span>
							</div>
						</div>
						<ArrowRight size={16} class="mt-2 shrink-0 text-app-muted transition group-hover:translate-x-0.5 group-hover:text-brand-strong" />
					</div>
				</a>
			{/each}
		</div>

		<nav class="mt-3 flex flex-wrap items-center gap-3 text-xs text-app-muted" aria-label="Discover pagination">
			<span class="tabular-nums">Showing {showingFrom}–{showingTo} of {data.notebooks.total}</span>
			<span class="ml-auto tabular-nums">Page {data.notebooks.page} of {totalPages}</span>
			<div class="flex items-center gap-1.5">
				{#if data.notebooks.page > 1}
					<Button intent="secondary" size="sm" href={pageHref(data.notebooks.page - 1)}><ChevronLeft size={14} />Previous</Button>
				{/if}
				{#if data.notebooks.page < totalPages}
					<Button intent="secondary" size="sm" href={pageHref(data.notebooks.page + 1)}>Next<ChevronRight size={14} /></Button>
				{/if}
			</div>
		</nav>
	{/if}
</section>
