<script lang="ts">
	import type { Snippet } from 'svelte';
	import type { HTMLAttributes } from 'svelte/elements';

	type Tone = 'neutral' | 'accent' | 'warn' | 'danger' | 'ok';

	type Props = {
		tone?: Tone;
		children?: Snippet;
	} & HTMLAttributes<HTMLSpanElement>;

	let { tone = 'neutral', class: className = '', children, ...rest }: Props = $props();
</script>

<span class="badge {tone} {className}" {...rest}>{@render children?.()}</span>

<style>
	.badge {
		display: inline-flex;
		align-items: center;
		gap: 0.25rem;
		font-size: 0.72rem;
		font-weight: 700;
		letter-spacing: 0.05em;
		line-height: 1.5;
		padding: 0.05rem 0.5rem;
		border-radius: var(--radius-pill);
		border: 1px solid var(--line);
		color: var(--muted);
		text-transform: uppercase;
		vertical-align: middle;
		white-space: nowrap;
	}
	.accent {
		color: var(--accent);
		border-color: var(--accent);
	}
	.warn {
		color: var(--accent2);
		border-color: var(--warn-line);
		background: var(--warn-bg);
	}
	.danger {
		color: var(--del);
		border-color: var(--del);
	}
	.ok {
		color: var(--accent);
		border-color: var(--note-line);
		background: var(--note-bg);
	}
</style>
