<script lang="ts">
	import type { Snippet } from 'svelte';
	import type { HTMLAttributes } from 'svelte/elements';

	// `note` and `warn` mirror Huron. `danger` carries the same left-rule shape for
	// errors. None of them sets a live-region role on its own: callers that render
	// an error or notice pass `role="alert"` / `role="status"` explicitly, so a page
	// never ends up with an extra alert landmark it did not ask for.
	type Props = {
		kind?: 'note' | 'warn' | 'danger';
		children?: Snippet;
	} & HTMLAttributes<HTMLDivElement>;

	let { kind = 'note', class: className = '', children, ...rest }: Props = $props();
</script>

<div class="callout {kind} {className}" {...rest}>
	{@render children?.()}
</div>

<style>
	.callout {
		border-left: 4px solid var(--note-line);
		background: var(--note-bg);
		color: var(--fg);
		padding: 0.7rem 1rem;
		border-radius: 0 var(--radius) var(--radius) 0;
		font-size: 0.875rem;
		line-height: 1.5;
	}
	.warn {
		border-left-color: var(--warn-line);
		background: var(--warn-bg);
	}
	.danger {
		border-left-color: var(--del);
		background: color-mix(in srgb, var(--del) 9%, transparent);
		color: var(--del);
	}
</style>
