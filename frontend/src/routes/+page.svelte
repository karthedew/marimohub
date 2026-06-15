<script lang="ts">
	import { mockApiEnabled } from '$lib/api';

	let { data } = $props();
	let notebooks = $derived(data.notebooks.items);
</script>

<section class="grid gap-10 lg:grid-cols-[1fr_24rem] lg:items-start">
	<div class="space-y-7">
		<p class="w-fit rounded-full bg-orange-100 px-4 py-2 text-sm font-semibold text-orange-900 dark:bg-orange-400/10 dark:text-orange-200">
			Phase 0 frontend scaffold
		</p>
		<div class="space-y-5">
			<h1 class="max-w-3xl text-5xl font-black tracking-tight text-slate-950 dark:text-white sm:text-7xl">Discover, fork, and run marimo notebooks.</h1>
			<p class="max-w-2xl text-lg leading-8 text-slate-700 dark:text-slate-300">
				MoLab is the collaboration shell for hosted notebooks, session-backed editing, and shareable deployments.
			</p>
		</div>
	</div>

	<aside class="rounded-[2rem] border border-slate-900/10 bg-white/70 p-6 shadow-xl shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20">
		<p class="text-sm font-semibold uppercase tracking-[0.24em] text-slate-500 dark:text-slate-400">API mode</p>
		<p class="mt-3 text-2xl font-bold">{mockApiEnabled ? 'Mock fixtures enabled' : 'Backend API enabled'}</p>
		<p class="mt-3 text-sm leading-6 text-slate-600 dark:text-slate-300">
			Fixture notebooks keep F3-F5 unblocked until backend cards merge. Set <code>PUBLIC_MOCK_API=false</code> with <code>PUBLIC_API_URL</code> to use the API.
		</p>
	</aside>
</section>

<section class="mt-14">
	<div class="mb-5 flex items-end justify-between gap-4">
		<div>
			<p class="text-sm font-semibold uppercase tracking-[0.24em] text-orange-700 dark:text-orange-300">Discover</p>
			<h2 class="mt-2 text-3xl font-bold">Featured notebooks</h2>
		</div>
		<p class="text-sm text-slate-600 dark:text-slate-400">{notebooks.length} shown</p>
	</div>

	<div class="grid gap-5 md:grid-cols-2">
		{#each notebooks as notebook (notebook.id)}
			<article class="rounded-[1.5rem] border border-slate-900/10 bg-white/75 p-6 shadow-sm transition hover:-translate-y-0.5 hover:shadow-lg dark:border-white/10 dark:bg-slate-950/50">
				<div class="flex items-start justify-between gap-4">
					<h3 class="text-xl font-bold">{notebook.title}</h3>
					<span class="rounded-full bg-slate-900 px-3 py-1 text-xs font-semibold text-white dark:bg-white dark:text-slate-950">{notebook.visibility}</span>
				</div>
				<p class="mt-3 min-h-12 text-sm leading-6 text-slate-600 dark:text-slate-300">{notebook.description}</p>
				<div class="mt-5 flex flex-wrap gap-2">
					{#each notebook.tags as tag}
						<span class="rounded-full bg-orange-100 px-3 py-1 text-xs font-semibold text-orange-900 dark:bg-orange-400/10 dark:text-orange-100">#{tag}</span>
					{/each}
				</div>
				<p class="mt-5 text-sm text-slate-500 dark:text-slate-400">{notebook.fork_count} forks</p>
			</article>
		{/each}
	</div>
</section>
