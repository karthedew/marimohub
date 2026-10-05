<script lang="ts">
	import { Moon, Sun } from '@lucide/svelte';
	import { applyTheme, type Theme } from '$lib/theme';

	// Self-contained like Huron's ThemeToggle: the inline script in `app.html`
	// already applied the saved `molab-theme` choice to <html> before first paint,
	// so the class on the root element is the source of truth to start from.
	let theme = $state<Theme>('light');

	$effect(() => {
		theme = document.documentElement.classList.contains('dark') ? 'dark' : 'light';
	});

	const next = $derived<Theme>(theme === 'dark' ? 'light' : 'dark');

	function toggle() {
		theme = next;
		applyTheme(theme);
	}
</script>

<button
	type="button"
	class="grid size-9 shrink-0 place-items-center rounded-md border border-app-line bg-app-card text-app-muted transition hover:border-app-line-strong hover:text-app-fg"
	aria-label={`Switch to ${next} mode`}
	title={`Switch to ${next} mode`}
	onclick={toggle}
>
	{#if theme === 'dark'}<Sun size={17} />{:else}<Moon size={17} />{/if}
</button>
