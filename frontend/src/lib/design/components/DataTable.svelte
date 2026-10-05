<script lang="ts">
	import type { Snippet } from 'svelte';

	type Props = {
		caption?: string;
		minWidth?: string;
		head?: Snippet;
		children?: Snippet;
	};

	let { caption, minWidth = '36rem', head, children }: Props = $props();
</script>

<!--
	A plain semantic table in Huron's list style: an uppercase header band and hairline
	row separators. Rows stay real `<tr>` elements so they remain addressable by role.
-->
<div class="scroll">
	<table style:min-width={minWidth}>
		{#if caption}<caption class="visually-hidden">{caption}</caption>{/if}
		{#if head}<thead>{@render head()}</thead>{/if}
		<tbody>{@render children?.()}</tbody>
	</table>
</div>

<style>
	.scroll {
		overflow-x: auto;
	}
	table {
		border-collapse: collapse;
		width: 100%;
		font-size: 0.875rem;
	}
	table :global(th) {
		background: var(--bg);
		border-bottom: 1px solid var(--line);
		padding: 0.6rem 1rem;
		font-size: 10px;
		font-weight: 600;
		letter-spacing: 0.09em;
		text-transform: uppercase;
		color: var(--muted);
		white-space: nowrap;
	}
	table :global(td) {
		border-bottom: 1px solid var(--line);
		padding: 0.75rem 1rem;
		vertical-align: middle;
	}
	table :global(tbody tr:last-child td) {
		border-bottom: 0;
	}
</style>
