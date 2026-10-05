<script lang="ts">
	import { Compass, House, TriangleAlert } from '@lucide/svelte';
	import { page } from '$app/state';
	import Button from '$lib/components/Button.svelte';
</script>

<svelte:head>
	<title>Page unavailable | MarimoHub</title>
</svelte:head>

<section class="mx-auto mt-6 max-w-xl rounded-lg border border-app-line bg-app-card p-8 text-center shadow-[var(--shadow-sm)] sm:p-10">
	<span class="mx-auto grid size-11 place-items-center rounded-lg bg-brand-soft text-brand-strong"><TriangleAlert size={20} /></span>
	<p class="mt-5 text-xs font-semibold uppercase tracking-[0.14em] text-app-muted">Error {page.status}</p>
	<h1 class="mt-2">Page unavailable</h1>
	<p class="mt-3 text-sm text-app-muted">
		{page.error?.message ?? 'Something went wrong while loading this page.'}
	</p>
	<div class="mt-7 flex flex-wrap justify-center gap-2">
		<Button intent="primary" href="/discover"><Compass size={15} />Browse notebooks</Button>
		<Button intent="secondary" href="/"><House size={15} />Go home</Button>
	</div>
</section>
