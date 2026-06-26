import { browser } from '$app/environment';

export type Theme = 'light' | 'dark';

const storageKey = 'molab-theme';

function preferredTheme(): Theme {
	if (!browser) return 'light';
	try {
		const stored = localStorage.getItem(storageKey);
		if (stored === 'light' || stored === 'dark') return stored;
	} catch {
		return 'light';
	}
	return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

export function applyTheme(theme: Theme) {
	if (!browser) return;
	document.documentElement.classList.toggle('dark', theme === 'dark');
	try {
		localStorage.setItem(storageKey, theme);
	} catch {
		return;
	}
}

export function getInitialTheme(): Theme {
	const theme = preferredTheme();
	if (browser) document.documentElement.classList.toggle('dark', theme === 'dark');
	return theme;
}
