<script lang="ts">
	import { untrack } from 'svelte';
	import { ApiError, api, deploymentProxyUrl } from '$lib/api';
	import { decideWakeStep, nextWakeDelay, type WakeAttemptResult } from '$lib/deploymentWake';
	import { LoaderCircle, RotateCcw, Rocket, TriangleAlert } from '@lucide/svelte';
	import BrandMark from '$lib/components/BrandMark.svelte';
	import Button from '$lib/components/Button.svelte';

	let { params } = $props();
	let ready = $state(false);
	let frameLoaded = $state(false);
	let error = $state<string | null>(null);
	let capacityExhausted = $state(false);
	let attempts = $state(0);

	const iframeSrc = $derived(deploymentProxyUrl(params.slug));

	const WAKE_TIMEOUT_MS = 20_000;

	// Bumped by every new attempt (a slug change or a manual Retry) so a
	// superseded attempt's in-flight request and pending backoff timer both
	// become inert instead of overwriting state a newer attempt already moved
	// past — the same discard discipline the notebook detail page's
	// deployment polling uses.
	let generation = 0;
	let pendingTimeout: ReturnType<typeof setTimeout> | undefined;

	function sleep(ms: number) {
		return new Promise<void>((resolve) => {
			pendingTimeout = setTimeout(() => {
				pendingTimeout = undefined;
				resolve();
			}, ms);
		});
	}

	async function wakeDeployment(slug: string) {
		const requestId = ++generation;
		ready = false;
		frameLoaded = false;
		error = null;
		capacityExhausted = false;
		attempts = 0;

		const startedAt = Date.now();
		let delayMs = 1000;

		for (;;) {
			attempts += 1;
			let result: WakeAttemptResult;
			try {
				await api.deployments.get(slug);
				result = { ok: true };
			} catch (caught) {
				result = caught instanceof ApiError
					? { ok: false, status: caught.status, detail: caught.detail }
					: { ok: false, status: 0, detail: 'Deployment did not wake up in time.' };
			}
			if (requestId !== generation) return;

			const decision = decideWakeStep(result, {
				elapsedMs: Date.now() - startedAt,
				timeoutMs: WAKE_TIMEOUT_MS,
				nextDelayMs: delayMs
			});

			if (decision.action === 'ready') {
				ready = true;
				return;
			}
			if (decision.action === 'capacity') {
				error = decision.message;
				capacityExhausted = true;
				return;
			}
			if (decision.action === 'stop') {
				error = decision.message;
				return;
			}

			delayMs = nextWakeDelay(delayMs);
			await sleep(decision.delayMs);
			if (requestId !== generation) return;
		}
	}

	$effect(() => {
		const slug = params.slug;
		// `wakeDeployment` reads and writes `$state` (`attempts`, `ready`, ...) in
		// its own synchronous prefix, before its first `await`. Left tracked,
		// that read-after-write inside an effect's own execution makes Svelte
		// treat the effect as dependent on state the effect itself just changed
		// and rerun it immediately, forever. `untrack` scopes the dependency
		// suppression to exactly that synchronous prefix — this effect's only
		// real dependency is `slug`.
		untrack(() => void wakeDeployment(slug));
		return () => {
			generation++;
			if (pendingTimeout !== undefined) {
				clearTimeout(pendingTimeout);
				pendingTimeout = undefined;
			}
		};
	});
</script>

<svelte:head>
	<title>{params.slug} | MarimoHub deployment</title>
</svelte:head>

<main id="main" class="fixed inset-0 z-50 bg-app-bg text-app-fg">
	{#if ready}
		<div class="relative h-full w-full">
			{#if !frameLoaded}
				<div class="absolute inset-0 z-10 grid place-items-center bg-app-bg px-6 text-center" aria-live="polite">
					<div class="w-full max-w-sm rounded-lg border border-app-line bg-app-card p-8 shadow-[var(--shadow-sm)]">
						<LoaderCircle size={28} class="mx-auto animate-spin text-brand-strong" />
						<p class="mt-5 text-xs font-semibold uppercase tracking-[0.14em] text-brand-strong">Opening app</p>
						<p class="mt-2 text-sm text-app-muted">The deployment is awake. Loading the notebook frame now.</p>
					</div>
				</div>
			{/if}
			<iframe class="h-full w-full border-0 bg-white" src={iframeSrc} title={`${params.slug} deployment`} onload={() => (frameLoaded = true)} onerror={() => { frameLoaded = true; ready = false; error = 'The deployment frame could not be loaded.'; }}></iframe>
		</div>
	{:else}
		<div class="grid h-full place-items-center overflow-y-auto px-5 py-10">
			<div class="w-full max-w-md">
				<a href="/" class="mb-8 flex w-fit items-center gap-2.5 text-app-fg no-underline" aria-label="MarimoHub home">
					<BrandMark />
					<span class="text-[17px] font-semibold tracking-[-0.02em]">MarimoHub</span>
				</a>
				<div class="rounded-lg border border-app-line bg-app-card p-7 shadow-[var(--shadow-sm)]">
					{#if error}
						<span class="grid size-10 place-items-center rounded-md bg-app-danger/10 text-app-danger"><TriangleAlert size={18} /></span>
						<p class="mt-5 text-xs font-semibold uppercase tracking-[0.14em] text-app-danger">Deployment unavailable</p>
						<h1 class="mt-2 text-[1.6rem]">Unable to open this app.</h1>
						{#if capacityExhausted}
							<p class="mt-2 text-sm leading-6 text-app-muted">This deployment is at capacity right now. Wait a moment and try again.</p>
						{:else}
							<p class="mt-2 text-sm leading-6 text-app-muted">{error}</p>
						{/if}
						<Button intent="primary" class="mt-6" type="button" onclick={() => void wakeDeployment(params.slug)}>
							<RotateCcw size={15} />Try again
						</Button>
					{:else}
						<span class="grid size-10 place-items-center rounded-md bg-brand-soft text-brand-strong"><Rocket size={18} /></span>
						<p class="mt-5 text-xs font-semibold uppercase tracking-[0.14em] text-brand-strong">Waking up...</p>
						<h1 class="mt-2 break-words text-[1.6rem]">Starting {params.slug}</h1>
						<p class="mt-2 text-sm leading-6 text-app-muted">Cold deployments can take a few seconds before the notebook is ready.</p>
						<div class="mt-6 flex items-center gap-2 border-t border-app-line pt-4 text-xs text-app-muted" aria-live="polite">
							<LoaderCircle size={14} class="animate-spin text-brand-strong" />
							<span>Attempt {attempts}</span>
						</div>
					{/if}
				</div>
			</div>
		</div>
	{/if}
</main>
