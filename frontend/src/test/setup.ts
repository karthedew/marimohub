export {};

// SvelteKit's client build reads `$env/dynamic/public` off a global that the
// real browser entry point populates from an inline script before any app
// module runs. Outside that bootstrap (i.e. under Vitest) the global is
// simply missing, so anything importing `$env/dynamic/public` — including
// `$lib/api` — throws on module load unless we seed it ourselves first.
declare global {
	// eslint-disable-next-line no-var
	var __sveltekit_dev: { env: Record<string, string> } | undefined;
}

globalThis.__sveltekit_dev ??= { env: {} };

// jsdom does not implement matchMedia. Theme detection reads it, so unit
// tests that touch theme or other browser-only code need a stub in place
// before any module under test runs.
if (!window.matchMedia) {
	window.matchMedia = (query: string) => ({
		matches: false,
		media: query,
		onchange: null,
		addListener: () => {},
		removeListener: () => {},
		addEventListener: () => {},
		removeEventListener: () => {},
		dispatchEvent: () => false
	});
}
