import { browser } from '$app/environment';

export type Theme = 'light' | 'dark';

const storageKey = 'molab-theme';

function preferredTheme(): Theme {
	if (!browser) return 'light';
	const stored = localStorage.getItem(storageKey);
	if (stored === 'light' || stored === 'dark') return stored;
	return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

export function applyTheme(theme: Theme) {
	if (!browser) return;
	document.documentElement.classList.toggle('dark', theme === 'dark');
	localStorage.setItem(storageKey, theme);
}

export function getInitialTheme(): Theme {
	const theme = preferredTheme();
	if (browser) document.documentElement.classList.toggle('dark', theme === 'dark');
	return theme;
}
