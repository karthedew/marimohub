<script lang="ts">
	import type { Snippet } from 'svelte';

	type Props = {
		href?: string;
		class?: string;
		children?: Snippet;
	};

	let { href, class: className = '', children }: Props = $props();
</script>

{#if href}
	<a class="card link {className}" {href}>{@render children?.()}</a>
{:else}
	<div class="card {className}">{@render children?.()}</div>
{/if}

<style>
	.card {
		display: block;
		border: 1px solid var(--line);
		background: var(--card);
		border-radius: 10px;
		padding: 1rem 1.1rem;
	}
	.link {
		text-decoration: none;
		color: inherit;
		transition: border-color 0.15s;
	}
	.link:hover {
		border-color: var(--accent);
	}
</style>
