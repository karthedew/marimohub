<svelte:head>
	<title>MoLab</title>
</svelte:head>

<section class="grid gap-10 lg:grid-cols-[minmax(0,1fr)_24rem] lg:items-center">
	<div class="space-y-8">
		<p class="w-fit rounded-full bg-orange-100 px-4 py-2 text-sm font-semibold text-orange-900 dark:bg-orange-400/10 dark:text-orange-200">
			Marimo notebooks, ready to share
		</p>
		<div class="space-y-5">
			<h1 class="max-w-3xl text-4xl font-black tracking-tight text-slate-950 dark:text-white sm:text-6xl lg:text-7xl">
				Discover, fork, and run interactive notebooks.
			</h1>
			<p class="max-w-2xl text-lg leading-8 text-slate-700 dark:text-slate-300">
				MoLab is a collaborative home for marimo notebooks, from research drafts to published demos and deployed apps.
			</p>
		</div>
		<div class="flex flex-wrap gap-3">
			<a class="rounded-full bg-graphite px-5 py-3 text-sm font-bold text-white shadow-lg shadow-slate-950/10 dark:bg-white dark:text-slate-950" href="/discover">
				Browse notebooks
			</a>
			<a class="rounded-full border border-slate-300/80 bg-white/60 px-5 py-3 text-sm font-bold text-slate-800 backdrop-blur hover:bg-white dark:border-white/15 dark:bg-white/10 dark:text-slate-100 dark:hover:bg-white/15" href="/notebooks/new">
				Create notebook
			</a>
		</div>
	</div>

	<aside class="rounded-[2rem] border border-slate-900/10 bg-white/75 p-6 shadow-xl shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20">
		<p class="text-sm font-semibold uppercase tracking-[0.24em] text-orange-700 dark:text-orange-300">Platform</p>
		<div class="mt-6 space-y-5">
			<div>
				<p class="text-3xl font-black">Open by default</p>
				<p class="mt-2 text-sm leading-6 text-slate-600 dark:text-slate-300">Public notebooks are easy to find, private drafts stay private, and unlisted work is simple to share.</p>
			</div>
			<div class="grid grid-cols-2 gap-3 text-sm font-semibold">
				<div class="rounded-2xl bg-orange-100 p-4 text-orange-950 dark:bg-orange-400/10 dark:text-orange-100">Discover</div>
				<div class="rounded-2xl bg-slate-900 p-4 text-white dark:bg-white dark:text-slate-950">Fork</div>
				<div class="rounded-2xl bg-white p-4 text-slate-800 shadow-sm dark:bg-white/10 dark:text-slate-100">Run</div>
				<div class="rounded-2xl bg-white p-4 text-slate-800 shadow-sm dark:bg-white/10 dark:text-slate-100">Deploy</div>
			</div>
		</div>
	</aside>
</section>
