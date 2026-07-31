<script lang="ts">
	import Button from '$lib/components/Button.svelte';

	let { data } = $props();

	function formatDate(value: string) {
		return new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric' }).format(new Date(value));
	}
</script>

<svelte:head>
	<title>{data.authenticated ? 'Notebooks | MarimoHub' : 'MarimoHub'}</title>
</svelte:head>

{#if data.authenticated}
	<section class="space-y-7">
		<div class="flex flex-col gap-4 border-b border-slate-900/10 pb-6 dark:border-white/10 sm:flex-row sm:items-end sm:justify-between">
			<div>
				<p class="text-sm font-semibold text-hub-700 dark:text-hub-300">{data.workspace?.name ?? 'Your notebooks'}</p>
				<h1 class="mt-1 text-3xl font-black tracking-tight text-slate-950 dark:text-white sm:text-4xl">Notebooks</h1>
				<p class="mt-2 text-sm text-slate-500 dark:text-slate-400">
					{#if data.workspace}
						{data.notebooks.total} {data.notebooks.total === 1 ? 'notebook' : 'notebooks'} in this Workspace
					{:else}
						Create or join a Workspace to begin
					{/if}
				</p>
			</div>
			{#if data.workspace && data.workspace.role !== 'viewer'}
				<Button intent="primary" size="sm" href={`/notebooks/new?workspace=${data.workspace.id}`}>New notebook</Button>
			{/if}
		</div>

		{#if data.error}
			<div class="rounded-2xl border border-red-200 bg-red-50 px-5 py-4 text-sm font-semibold text-red-800 dark:border-red-400/20 dark:bg-red-500/10 dark:text-red-200" role="alert">{data.error}</div>
		{:else if !data.workspace}
			<div class="rounded-2xl border border-slate-900/10 bg-white p-8 text-center dark:border-white/10 dark:bg-[#181a1b]">
				<h2 class="text-xl font-bold text-slate-950 dark:text-white">Create your first Workspace</h2>
				<p class="mx-auto mt-2 max-w-md text-sm leading-6 text-slate-500 dark:text-slate-400">Workspaces are where Notebooks live and where you manage who can access them.</p>
				<Button class="mt-5" size="sm" href="/workspaces?create=1">Create workspace</Button>
			</div>
		{:else if data.notebooks.items.length === 0}
			<div class="rounded-2xl border border-dashed border-slate-300 bg-white/60 p-10 text-center dark:border-white/15 dark:bg-[#181a1b]">
				<h2 class="text-xl font-bold text-slate-950 dark:text-white">No notebooks yet</h2>
				<p class="mt-2 text-sm text-slate-500 dark:text-slate-400">Start from a blank marimo notebook, upload a Python file, or import from GitLab.</p>
				{#if data.workspace.role !== 'viewer'}
					<Button class="mt-5" size="sm" href={`/notebooks/new?workspace=${data.workspace.id}`}>Create notebook</Button>
				{/if}
			</div>
		{:else}
			<div class="overflow-hidden rounded-2xl border border-slate-900/10 bg-white shadow-sm dark:border-white/10 dark:bg-[#181a1b]">
				{#each data.notebooks.items as notebook (notebook.id)}
					<a class="group flex items-center gap-4 border-b border-slate-900/10 px-4 py-4 transition last:border-b-0 hover:bg-slate-50 dark:border-white/10 dark:hover:bg-white/[0.035] sm:px-5" href={`/notebooks/${notebook.id}`}>
						<span class="grid size-10 shrink-0 place-items-center rounded-xl bg-slate-100 text-hub-700 dark:bg-slate-800 dark:text-hub-300">
							<svg class="size-5" viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M5 2.75h6.5L15 6.25v11H5v-14Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M11.5 2.75v3.5H15M7.5 10h5M7.5 13h3.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
						</span>
						<span class="min-w-0 flex-1">
							<span class="block truncate font-bold text-slate-900 transition group-hover:text-hub-700 dark:text-slate-100 dark:group-hover:text-hub-300">{notebook.title}</span>
							<span class="mt-1 block truncate text-sm text-slate-500 dark:text-slate-400">{notebook.description ?? 'No description'}</span>
						</span>
						<span class="hidden rounded-md bg-slate-100 px-2 py-1 text-xs font-semibold capitalize text-slate-500 dark:bg-slate-800 dark:text-slate-400 sm:block">{notebook.visibility}</span>
						<span class="hidden w-20 text-right text-xs text-slate-400 md:block">{formatDate(notebook.updated_at)}</span>
						<svg class="size-4 shrink-0 text-slate-300 transition group-hover:translate-x-0.5 group-hover:text-slate-500 dark:text-slate-600" viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="m8 5 5 5-5 5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>
					</a>
				{/each}
			</div>
		{/if}
	</section>
{:else}
<section class="grid gap-10 lg:grid-cols-[minmax(0,1fr)_24rem] lg:items-center">
	<div class="space-y-8">
		<p class="w-fit rounded-full bg-hub-50 px-4 py-2 text-sm font-semibold text-hub-950 dark:bg-hub-400/10 dark:text-hub-200">
			Marimo notebooks, ready to share
		</p>
		<div class="space-y-5">
			<h1 class="max-w-3xl text-4xl font-black tracking-tight text-slate-950 dark:text-white sm:text-6xl lg:text-7xl">
				Discover, fork, and run interactive notebooks.
			</h1>
			<p class="max-w-2xl text-lg leading-8 text-slate-700 dark:text-slate-300">
				MarimoHub is a collaborative home for marimo notebooks, from private research notebooks to published demos and deployed apps.
			</p>
		</div>
		<div class="flex flex-wrap gap-3">
			<Button intent="primary" href="/discover">Browse notebooks</Button>
			<Button intent="secondary" href="/notebooks/new">Create notebook</Button>
		</div>
	</div>

	<aside class="rounded-[2rem] border border-slate-900/10 bg-white/75 p-6 shadow-xl shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20">
		<p class="text-sm font-semibold uppercase tracking-[0.24em] text-hub-700 dark:text-hub-300">Platform</p>
		<div class="mt-6 space-y-5">
			<div>
				<p class="text-3xl font-black">Open by default</p>
				<p class="mt-2 text-sm leading-6 text-slate-600 dark:text-slate-300">Public notebooks are easy to find, private notebooks stay inside their workspace, and unlisted work is simple to share by direct link.</p>
			</div>
			<div class="grid grid-cols-2 gap-3 text-sm font-semibold">
				<div class="rounded-2xl bg-hub-50 p-4 text-hub-950 dark:bg-hub-400/10 dark:text-hub-50">Discover</div>
				<div class="rounded-2xl bg-slate-900 p-4 text-white dark:bg-white dark:text-slate-950">Fork</div>
				<div class="rounded-2xl bg-white p-4 text-slate-800 shadow-sm dark:bg-white/10 dark:text-slate-100">Run</div>
				<div class="rounded-2xl bg-white p-4 text-slate-800 shadow-sm dark:bg-white/10 dark:text-slate-100">Deploy</div>
			</div>
		</div>
	</aside>
</section>
{/if}
