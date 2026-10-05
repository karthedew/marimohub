<script lang="ts">
	import { Compass, FileX } from '@lucide/svelte';
	import { page } from '$app/state';
	import Button from '$lib/components/Button.svelte';
</script>

<svelte:head>
	<title>Notebook unavailable | MarimoHub</title>
</svelte:head>

<section class="mx-auto mt-6 max-w-xl rounded-lg border border-app-line bg-app-card p-8 text-center shadow-[var(--shadow-sm)] sm:p-10">
	<span class="mx-auto grid size-11 place-items-center rounded-lg bg-brand-soft text-brand-strong"><FileX size={20} /></span>
	<p class="mt-5 text-xs font-semibold uppercase tracking-[0.14em] text-app-muted">Error {page.status}</p>
	<h1 class="mt-2">Notebook unavailable</h1>
	<p class="mt-3 text-sm text-app-muted">
		{page.error?.message ?? 'Unable to load this notebook.'}
	</p>
	<Button intent="primary" class="mt-7" href="/discover"><Compass size={15} />Browse notebooks</Button>
</section>
