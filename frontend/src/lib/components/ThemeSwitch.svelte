<script lang="ts">
	import type { Theme } from '$lib/theme';

	type Props = {
		theme: Theme;
		onchange: () => void;
	};

	let { theme, onchange }: Props = $props();
</script>

<button
	type="button"
	class="group inline-flex items-center gap-2 rounded-full p-1 text-xs font-semibold text-slate-600 outline-none transition hover:text-slate-950 focus-visible:ring-4 focus-visible:ring-hub-600/20 dark:text-slate-400 dark:hover:text-white"
	aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}
	aria-pressed={theme === 'dark'}
	onclick={onchange}
>
	<span class="hidden sm:inline">{theme === 'dark' ? 'Dark' : 'Light'}</span>
	<span class="relative h-6 w-11 rounded-full bg-slate-300 transition-colors group-hover:bg-slate-400 dark:bg-slate-700 dark:group-hover:bg-slate-600">
		<span class={`absolute top-1 grid size-4 place-items-center rounded-full bg-white shadow-sm transition-transform ${theme === 'dark' ? 'translate-x-6' : 'translate-x-1'}`}>
			<span class="size-1.5 rounded-full bg-hub-700"></span>
		</span>
	</span>
</button>
