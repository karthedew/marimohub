<script lang="ts">
	import { Compass, GitFork, Rocket } from '@lucide/svelte';
	import type { Snippet } from 'svelte';
	import BrandMark from '$lib/components/BrandMark.svelte';
	import ThemeSwitch from '$lib/components/ThemeSwitch.svelte';

	type Props = {
		eyebrow: string;
		title: string;
		detail: string;
		children: Snippet;
	};

	let { eyebrow, title, detail, children }: Props = $props();
</script>

<div class="grid min-h-dvh bg-app-bg text-app-fg lg:grid-cols-[minmax(360px,0.85fr)_1.15fr]">
	<aside class="relative hidden overflow-hidden border-r border-app-line bg-[#062d29] p-10 text-white lg:flex lg:flex-col xl:p-14" aria-label="About MarimoHub">
		<div class="pointer-events-none absolute -right-24 -top-24 size-80 rounded-full bg-brand/15 blur-3xl"></div>
		<div class="pointer-events-none absolute -bottom-32 -left-24 size-96 rounded-full bg-[#167a70]/30 blur-3xl"></div>

		<a href="/" class="relative flex w-fit items-center gap-3 text-white no-underline" aria-label="MarimoHub home">
			<BrandMark size="lg" onDark />
			<span class="text-xl font-semibold tracking-[-0.025em]">MarimoHub</span>
		</a>

		<div class="relative my-auto max-w-lg py-16">
			<p class="text-xs font-semibold uppercase tracking-[0.15em] text-brand">Marimo notebooks</p>
			<p class="mt-4 text-4xl font-semibold leading-[1.1] tracking-[-0.04em] xl:text-5xl">
				Notebooks you can fork, run, and deploy.
			</p>
			<p class="mt-5 max-w-md text-base leading-7 text-white/65">
				A collaborative home for marimo notebooks, from private research to published demos and deployed apps.
			</p>

			<ul class="m-0 mt-9 grid list-none gap-4 p-0 text-sm text-white/80">
				<li class="flex items-center gap-3">
					<span class="grid size-8 place-items-center rounded-md bg-white/10 text-brand"><Compass size={16} /></span>Discover Public notebooks across the hub
				</li>
				<li class="flex items-center gap-3">
					<span class="grid size-8 place-items-center rounded-md bg-white/10 text-brand"><GitFork size={16} /></span>Fork into any Workspace you can write to
				</li>
				<li class="flex items-center gap-3">
					<span class="grid size-8 place-items-center rounded-md bg-white/10 text-brand"><Rocket size={16} /></span>Deploy a Notebook as a public app
				</li>
			</ul>
		</div>

		<p class="relative text-xs text-white/45">MarimoHub hosts forkable marimo notebooks for research, demos, and deployed apps.</p>
	</aside>

	<main id="main" class="relative flex min-h-dvh items-center justify-center px-5 py-10 sm:px-8">
		<div class="absolute right-4 top-4 sm:right-6 sm:top-6">
			<ThemeSwitch />
		</div>
		<div class="w-full max-w-[440px]">
			<a href="/" class="mb-10 flex w-fit items-center gap-2.5 text-app-fg no-underline lg:hidden" aria-label="MarimoHub home">
				<BrandMark size="md" />
				<span class="text-lg font-semibold tracking-[-0.02em]">MarimoHub</span>
			</a>

			<p class="text-xs font-semibold uppercase tracking-[0.14em] text-brand-strong">{eyebrow}</p>
			<h1 class="mt-2 text-[2rem] tracking-[-0.035em]">{title}</h1>
			<p class="mt-2 text-sm leading-6 text-app-muted">{detail}</p>

			<div class="mt-7">{@render children()}</div>
		</div>
	</main>
</div>
