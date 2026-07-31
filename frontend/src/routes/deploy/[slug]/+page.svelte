<script lang="ts">
	import { untrack } from 'svelte';
	import { ApiError, api, deploymentProxyUrl } from '$lib/api';
	import { decideWakeStep, nextWakeDelay, type WakeAttemptResult } from '$lib/deploymentWake';
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

<div class="fixed inset-0 z-50 bg-slate-950 text-white">
	{#if ready}
		<div class="relative h-full w-full">
			{#if !frameLoaded}
				<div class="absolute inset-0 z-10 grid place-items-center bg-slate-950 px-6 text-center" aria-live="polite">
					<div class="w-full max-w-md rounded-[2rem] border border-white/10 bg-white/10 p-8 shadow-2xl shadow-black/40 backdrop-blur">
						<div class="mx-auto h-3 w-40 overflow-hidden rounded-full bg-white/10">
							<div class="h-full w-1/2 animate-pulse rounded-full bg-hub-300"></div>
						</div>
						<p class="mt-7 text-sm font-semibold uppercase tracking-[0.3em] text-hub-200">Opening app</p>
						<p class="mt-4 text-sm leading-6 text-slate-300">The deployment is awake. Loading the notebook frame now.</p>
					</div>
				</div>
			{/if}
			<iframe class="h-full w-full border-0 bg-white" src={iframeSrc} title={`${params.slug} deployment`} onload={() => (frameLoaded = true)} onerror={() => { frameLoaded = true; ready = false; error = 'The deployment frame could not be loaded.'; }}></iframe>
		</div>
	{:else}
		<div class="grid h-full place-items-center px-6">
			<div class="w-full max-w-xl rounded-[2rem] border border-white/10 bg-white/10 p-8 text-center shadow-2xl shadow-black/40 backdrop-blur">
				{#if error}
					<p class="text-sm font-semibold uppercase tracking-[0.3em] text-red-200">Deployment unavailable</p>
					<h1 class="mt-5 text-3xl font-black tracking-tight sm:text-5xl">Unable to open this app.</h1>
					{#if capacityExhausted}
						<p class="mt-4 text-sm leading-6 text-slate-300">This deployment is at capacity right now. Wait a moment and try again.</p>
					{:else}
						<p class="mt-4 text-sm leading-6 text-slate-300">{error}</p>
					{/if}
					<Button intent="primary" onDark class="mt-7" type="button" onclick={() => void wakeDeployment(params.slug)}>
						Try again
					</Button>
				{:else}
					<div class="mx-auto h-3 w-40 overflow-hidden rounded-full bg-white/10">
						<div class="h-full w-1/2 animate-pulse rounded-full bg-hub-300"></div>
					</div>
					<p class="mt-7 text-sm font-semibold uppercase tracking-[0.3em] text-hub-200">Waking up...</p>
					<h1 class="mt-5 text-3xl font-black tracking-tight sm:text-5xl">Starting {params.slug}</h1>
					<p class="mt-4 text-sm leading-6 text-slate-300">Cold deployments can take a few seconds before the notebook is ready.</p>
					<p class="mt-5 text-xs font-semibold uppercase tracking-[0.22em] text-slate-500">Attempt {attempts}</p>
				{/if}
			</div>
		</div>
	{/if}
</div>
