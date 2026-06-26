<script lang="ts">
	let { data } = $props();
	let loading = $state(false);

	const totalPages = $derived(Math.max(1, Math.ceil(data.notebooks.total / data.notebooks.page_size)));
	const showingFrom = $derived(data.notebooks.total === 0 ? 0 : (data.notebooks.page - 1) * data.notebooks.page_size + 1);
	const showingTo = $derived(Math.min(data.notebooks.page * data.notebooks.page_size, data.notebooks.total));

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
	<title>Discover | MoLab</title>
</svelte:head>

<section class="space-y-8">
	<div class="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
		<div class="space-y-3">
			<p class="w-fit rounded-full bg-orange-100 px-4 py-2 text-sm font-semibold text-orange-900 dark:bg-orange-400/10 dark:text-orange-200">
				Discover
			</p>
			<h1 class="text-4xl font-black tracking-tight text-slate-950 dark:text-white sm:text-6xl">Browse notebooks</h1>
			<p class="max-w-2xl text-slate-700 dark:text-slate-300">
				Find public and unlisted marimo notebooks ready to run, fork, and deploy.
			</p>
		</div>
	</div>

	<form
		class="rounded-[2rem] border border-slate-900/10 bg-white/75 p-5 shadow-lg shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20"
		method="GET"
		onsubmit={() => {
			loading = true;
		}}
	>
		<div class="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(13rem,0.45fr)_auto] lg:items-end">
			<div>
				<label class="text-sm font-bold text-slate-800 dark:text-slate-100" for="q">Search</label>
				<input
					class="mt-2 w-full rounded-2xl border border-slate-300 bg-white px-4 py-3 text-slate-950 outline-none transition focus:border-orange-500 focus:ring-4 focus:ring-orange-500/15 dark:border-white/15 dark:bg-slate-950/50 dark:text-white"
					id="q"
					name="q"
					type="search"
					value={data.filters.q}
					placeholder="Signals, dashboards, optimization"
				/>
			</div>

			<div>
				<label class="text-sm font-bold text-slate-800 dark:text-slate-100" for="tags">Tags</label>
				<input
					class="mt-2 w-full rounded-2xl border border-slate-300 bg-white px-4 py-3 text-slate-950 outline-none transition focus:border-orange-500 focus:ring-4 focus:ring-orange-500/15 dark:border-white/15 dark:bg-slate-950/50 dark:text-white"
					id="tags"
					name="tags"
					type="text"
					value={data.filters.tags}
					placeholder="grafana, iot"
				/>
			</div>

			<button class="rounded-full bg-graphite px-5 py-3 text-sm font-bold text-white shadow-lg shadow-slate-950/10 disabled:cursor-wait disabled:opacity-70 dark:bg-white dark:text-slate-950" type="submit" disabled={loading}>
				{loading ? 'Searching...' : 'Search'}
			</button>
		</div>

		<label class="mt-4 flex w-fit items-center gap-3 rounded-full bg-slate-100 px-4 py-2 text-sm font-semibold text-slate-700 dark:bg-white/10 dark:text-slate-200">
			<input class="size-4 accent-orange-600" type="checkbox" name="semantic" value="1" checked={data.filters.semantic} />
			Semantic search
		</label>
	</form>

	{#if data.error}
		<div class="rounded-[1.5rem] border border-red-200 bg-red-50 px-5 py-4 text-sm text-red-800 dark:border-red-400/20 dark:bg-red-500/10 dark:text-red-200" role="alert">
			<p class="font-black">Unable to load notebooks</p>
			<p class="mt-1 font-semibold">{data.error}</p>
			<a class="mt-4 inline-flex rounded-full bg-red-900 px-4 py-2 text-xs font-black text-white dark:bg-red-100 dark:text-red-950" href={pageHref(data.notebooks.page)} onclick={() => (loading = true)}>Try again</a>
		</div>
	{:else if loading}
		<div class="grid gap-4 md:grid-cols-2" aria-label="Loading notebooks">
			{#each Array(4) as _}
				<div class="h-56 animate-pulse rounded-[1.75rem] border border-slate-900/10 bg-white/55 dark:border-white/10 dark:bg-white/10"></div>
			{/each}
		</div>
	{:else if data.notebooks.items.length === 0}
		<div class="rounded-[2rem] border border-slate-900/10 bg-white/75 p-8 text-center shadow-lg shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20">
			<p class="text-2xl font-black text-slate-950 dark:text-white">No notebooks found</p>
			<p class="mt-2 text-sm leading-6 text-slate-600 dark:text-slate-300">Try a broader search or remove tag filters.</p>
		</div>
	{:else}
		<div class="flex flex-col gap-3 text-sm font-semibold text-slate-600 dark:text-slate-300 sm:flex-row sm:items-center sm:justify-between">
			<p>Showing {showingFrom}-{showingTo} of {data.notebooks.total}</p>
			<p>Page {data.notebooks.page} of {totalPages}</p>
		</div>

		<div class="grid gap-4 md:grid-cols-2">
			{#each data.notebooks.items as notebook}
				<a
					class="group rounded-[1.75rem] border border-slate-900/10 bg-white/75 p-6 shadow-lg shadow-slate-900/5 backdrop-blur transition hover:-translate-y-0.5 hover:shadow-xl dark:border-white/10 dark:bg-white/10 dark:shadow-black/20"
					href={`/notebooks/${notebook.id}`}
				>
					<div class="flex items-start justify-between gap-4">
						<div>
							<p class="text-sm font-semibold uppercase tracking-[0.2em] text-orange-700 dark:text-orange-300">{notebook.visibility}</p>
							<h2 class="mt-3 text-2xl font-black text-slate-950 group-hover:text-orange-700 dark:text-white dark:group-hover:text-orange-200">
								{notebook.title}
							</h2>
						</div>
						<span class="rounded-full bg-slate-900 px-3 py-1 text-sm font-bold text-white dark:bg-white dark:text-slate-950">
							{notebook.fork_count} forks
						</span>
					</div>

					<p class="mt-4 line-clamp-2 text-sm leading-6 text-slate-600 dark:text-slate-300">{notebook.description ?? 'No description provided.'}</p>

					<div class="mt-5 flex flex-wrap gap-2">
						{#if notebook.tags.length > 0}
							{#each notebook.tags as tag}
								<span class="rounded-full bg-slate-100 px-3 py-1 text-xs font-semibold text-slate-700 dark:bg-white/10 dark:text-slate-200">{tag}</span>
							{/each}
						{:else}
							<span class="rounded-full bg-slate-100 px-3 py-1 text-xs font-semibold text-slate-500 dark:bg-white/10 dark:text-slate-400">No tags</span>
						{/if}
					</div>
				</a>
			{/each}
		</div>

		<div class="flex items-center justify-between gap-3">
			{#if data.notebooks.page > 1}
				<a class="rounded-full border border-slate-300/80 bg-white/60 px-5 py-3 text-sm font-bold text-slate-800 backdrop-blur hover:bg-white dark:border-white/15 dark:bg-white/10 dark:text-slate-100 dark:hover:bg-white/15" href={pageHref(data.notebooks.page - 1)} onclick={() => (loading = true)}>Previous</a>
			{:else}
				<span></span>
			{/if}

			{#if data.notebooks.page < totalPages}
				<a class="rounded-full bg-graphite px-5 py-3 text-sm font-bold text-white shadow-lg shadow-slate-950/10 dark:bg-white dark:text-slate-950" href={pageHref(data.notebooks.page + 1)} onclick={() => (loading = true)}>Next</a>
			{/if}
		</div>
	{/if}
</section>
