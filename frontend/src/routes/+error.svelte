<script lang="ts">
	import { page } from '$app/state';
	import Button from '$lib/components/Button.svelte';
</script>

<svelte:head>
	<title>Page unavailable | MarimoHub</title>
</svelte:head>

<section class="mx-auto max-w-2xl rounded-[2rem] border border-slate-900/10 bg-white/80 p-8 text-center shadow-xl shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20 sm:p-10">
	<p class="mx-auto w-fit rounded-full bg-hub-50 px-4 py-2 text-sm font-semibold text-hub-950 dark:bg-hub-400/10 dark:text-hub-200">
		{page.status}
	</p>
	<h1 class="mt-5 text-4xl font-black tracking-tight text-slate-950 dark:text-white">Page unavailable</h1>
	<p class="mt-4 text-slate-700 dark:text-slate-300">
		{page.error?.message ?? 'Something went wrong while loading this page.'}
	</p>
	<div class="mt-7 flex flex-wrap justify-center gap-3">
		<Button intent="primary" href="/discover">Browse notebooks</Button>
		<Button intent="secondary" href="/">Go home</Button>
	</div>
</section>
