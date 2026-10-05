<script lang="ts">
	// MarimoHub's own icon asset in Huron's logo slot. The two image files are the
	// one place a `dark:` swap is unavoidable: an <img> source cannot read a token.
	type Props = {
		size?: 'sm' | 'md' | 'lg';
		// Forces the dark-surface artwork regardless of theme (e.g. the AuthShell
		// panel, which is always dark).
		onDark?: boolean;
		class?: string;
	};

	let { size = 'md', onDark = false, class: className = '' }: Props = $props();

	const sizes = { sm: 'size-7', md: 'size-8', lg: 'size-10' } as const;
</script>

<span class="relative inline-grid shrink-0 place-items-center {sizes[size]} {className}" aria-hidden="true">
	{#if onDark}
		<img class="size-full" src="/assets/marimohub-icon-dark-transparent.svg" alt="" />
	{:else}
		<img class="size-full dark:hidden" src="/assets/marimohub-icon-light.svg" alt="" />
		<img class="hidden size-full dark:block" src="/assets/marimohub-icon-dark-transparent.svg" alt="" />
	{/if}
</span>
