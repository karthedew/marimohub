<script lang="ts">
	import type { LucideIcon } from '@lucide/svelte';
	import type { Snippet } from 'svelte';

	type Props = {
		title: string;
		detail?: string;
		icon?: LucideIcon;
		// The title is a plain paragraph by default, as in Huron. Pass `h2` when the
		// empty state is the page's main content and should be a navigable heading.
		titleAs?: 'p' | 'h2';
		children?: Snippet;
	};

	let { title, detail, icon: Icon, titleAs = 'p', children }: Props = $props();
</script>

<div class="empty">
	{#if Icon}<Icon size={24} class="mx-auto mb-3 text-app-muted" />{/if}
	<svelte:element this={titleAs} class="title">{title}</svelte:element>
	{#if detail}<p class="detail">{detail}</p>{/if}
	{#if children}<div class="actions">{@render children()}</div>{/if}
</div>

<style>
	.empty {
		border: 1px dashed var(--line-strong);
		border-radius: var(--radius);
		background: var(--card);
		padding: 2.5rem 1.5rem;
		text-align: center;
		color: var(--muted);
	}
	.title {
		margin: 0 0 0.3rem;
		padding: 0;
		border: 0;
		font-size: 0.95rem;
		font-weight: 600;
		color: var(--fg);
	}
	.detail {
		margin: 0 auto;
		max-width: 28rem;
		font-size: 0.85rem;
	}
	.actions {
		margin-top: 1.1rem;
		display: flex;
		flex-wrap: wrap;
		justify-content: center;
		gap: 0.5rem;
	}
</style>
