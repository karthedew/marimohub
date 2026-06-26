<script lang="ts">
	import { page } from '$app/state';
</script>

<svelte:head>
	<title>Notebook unavailable | MoLab</title>
</svelte:head>

<section class="mx-auto max-w-2xl rounded-[2rem] border border-slate-900/10 bg-white/80 p-8 text-center shadow-xl shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20">
	<p class="mx-auto w-fit rounded-full bg-orange-100 px-4 py-2 text-sm font-semibold text-orange-900 dark:bg-orange-400/10 dark:text-orange-200">
		{page.status}
	</p>
	<h1 class="mt-5 text-4xl font-black tracking-tight text-slate-950 dark:text-white">Notebook unavailable</h1>
	<p class="mt-4 text-slate-700 dark:text-slate-300">
		{page.error?.message ?? 'Unable to load this notebook.'}
	</p>
	<a class="mt-7 inline-flex rounded-full bg-graphite px-5 py-3 text-sm font-bold text-white shadow-lg shadow-slate-950/10 dark:bg-white dark:text-slate-950" href="/discover">
		Browse notebooks
	</a>
</section>
