<script lang="ts">
	import type { Snippet } from 'svelte';
	import type { HTMLAnchorAttributes, HTMLButtonAttributes } from 'svelte/elements';

	type Intent = 'primary' | 'secondary' | 'ghost' | 'danger' | 'ink';
	type Size = 'sm' | 'md' | 'lg';

	type Props = {
		intent?: Intent;
		size?: Size;
		href?: string;
		class?: string;
		children: Snippet;
	} & Omit<HTMLButtonAttributes, 'class' | 'children'> &
		Omit<HTMLAnchorAttributes, 'class' | 'children'>;

	let { intent = 'primary', size = 'md', href, class: className = '', children, ...rest }: Props = $props();

	const base =
		'inline-flex shrink-0 items-center justify-center gap-2 whitespace-nowrap rounded-md font-semibold no-underline transition disabled:cursor-not-allowed disabled:opacity-50';

	const sizes: Record<Size, string> = {
		sm: 'h-8 px-3 text-[13px]',
		md: 'h-9 px-3.5 text-sm',
		lg: 'h-11 px-4 text-sm'
	};

	const intents: Record<Intent, string> = {
		primary: 'bg-brand text-on-brand hover:brightness-95 disabled:hover:brightness-100',
		secondary:
			'border border-app-line bg-app-card text-app-fg shadow-[var(--shadow-sm)] hover:border-app-line-strong hover:bg-app-sidebar-hover disabled:hover:border-app-line disabled:hover:bg-app-card',
		ghost: 'text-app-muted hover:bg-app-sidebar-hover hover:text-app-fg',
		danger: 'bg-app-danger text-on-danger hover:brightness-95 disabled:hover:brightness-100',
		ink: 'bg-app-ink text-app-on-ink hover:brightness-110 disabled:hover:brightness-100'
	};

	const classes = $derived(`${base} ${sizes[size]} ${intents[intent]} ${className}`.trim());
</script>

{#if href}
	<a {href} class={classes} {...rest}>{@render children()}</a>
{:else}
	<button class={classes} {...rest}>{@render children()}</button>
{/if}
