import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vitest/config';

export default defineConfig({
	plugins: [tailwindcss(), sveltekit()],
	test: {
		environment: 'jsdom',
		setupFiles: ['./src/test/setup.ts'],
		include: ['src/**/*.{test,spec}.{js,ts}'],
		// jsdom's html-encoding-sniffer pulls in the ESM-only @exodus/bytes via a
		// plain `require()`. Node resolves that fine in the main process, but not
		// inside vitest's default forked child processes on this toolchain, so
		// the whole suite fails to even start under `pool: 'forks'`.
		pool: 'threads'
	}
});
