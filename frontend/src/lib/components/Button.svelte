<script lang="ts">
	import type { Snippet } from 'svelte';
	import type { HTMLAnchorAttributes, HTMLButtonAttributes } from 'svelte/elements';

	type Intent = 'primary' | 'secondary' | 'ghost';
	type Size = 'sm' | 'md';

	type Props = {
		intent?: Intent;
		size?: Size;
		onDark?: boolean;
		href?: string;
		class?: string;
		children: Snippet;
	} & Omit<HTMLButtonAttributes, 'class' | 'children'> &
		Omit<HTMLAnchorAttributes, 'class' | 'children'>;

	let {
		intent = 'primary',
		size = 'md',
		onDark = false,
		href,
		class: className = '',
		children,
		...rest
	}: Props = $props();

	const base =
		'inline-flex items-center justify-center rounded-full font-bold transition focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-hub-500/35 disabled:cursor-not-allowed disabled:opacity-60';

	const sizes: Record<Size, string> = {
		sm: 'px-4 py-2 text-sm',
		md: 'px-5 py-3 text-sm'
	};

	const intents: Record<Intent, string> = {
		primary:
			'bg-hub-700 text-white shadow-lg shadow-hub-950/15 hover:bg-hub-800 dark:bg-hub-500 dark:text-hub-950 dark:shadow-black/30 dark:hover:bg-hub-400',
		secondary:
			'border border-hub-700/25 bg-hub-50 text-hub-900 hover:bg-hub-100 dark:border-hub-300/20 dark:bg-hub-500/10 dark:text-hub-100 dark:hover:bg-hub-500/20',
		ghost: 'text-hub-900 hover:bg-hub-700/10 dark:text-hub-100 dark:hover:bg-hub-400/10'
	};

	const onDarkIntents: Record<Intent, string> = {
		primary: 'bg-hub-300 text-hub-950 shadow-lg shadow-black/20 hover:bg-hub-200',
		secondary: 'border border-hub-200/25 bg-hub-950/40 text-hub-50 hover:bg-hub-900/70',
		ghost: 'text-hub-100 hover:bg-hub-300/10'
	};

	const palette = $derived(onDark ? onDarkIntents : intents);
	const classes = $derived(`${base} ${sizes[size]} ${palette[intent]} ${className}`.trim());
</script>

{#if href}
	<a {href} class={classes} {...rest}>{@render children()}</a>
{:else}
	<button class={classes} {...rest}>{@render children()}</button>
{/if}
