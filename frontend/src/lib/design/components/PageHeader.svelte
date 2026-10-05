<script lang="ts">
	import type { LucideIcon } from '@lucide/svelte';
	import type { Snippet } from 'svelte';

	// Huron's page header: an uppercase eyebrow with an icon, the page's single h1,
	// a muted description, and right-aligned actions that stack under it on phones.
	type Props = {
		title: string;
		eyebrow?: string;
		icon?: LucideIcon;
		description?: Snippet | string;
		actions?: Snippet;
		heading?: Snippet;
	};

	let { title, eyebrow, icon: Icon, description, actions, heading }: Props = $props();
</script>

<div class="mb-7 flex flex-col justify-between gap-4 sm:flex-row sm:items-end">
	<div class="min-w-0">
		{#if eyebrow}
			<div class="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.11em] text-app-muted">
				{#if Icon}<Icon size={14} />{/if}
				<span class="truncate">{eyebrow}</span>
			</div>
		{/if}
		{#if heading}
			{@render heading()}
		{:else}
			<h1 class="break-words">{title}</h1>
		{/if}
		{#if description}
			<div class="mt-2 max-w-2xl text-sm leading-6 text-app-muted">
				{#if typeof description === 'string'}{description}{:else}{@render description()}{/if}
			</div>
		{/if}
	</div>
	{#if actions}
		<div class="flex shrink-0 flex-wrap items-center gap-2">{@render actions()}</div>
	{/if}
</div>
