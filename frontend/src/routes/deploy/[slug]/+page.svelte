<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { ApiError, api, deploymentProxyUrl } from '$lib/api';

	let { params } = $props();
	let ready = $state(false);
	let frameLoaded = $state(false);
	let error = $state<string | null>(null);
	let attempts = $state(0);
	let cancelled = false;

	const iframeSrc = $derived(deploymentProxyUrl(params.slug));

	function sleep(ms: number) {
		return new Promise((resolve) => setTimeout(resolve, ms));
	}

	async function wakeDeployment() {
		ready = false;
		frameLoaded = false;
		error = null;
		attempts = 0;

		for (let attempt = 1; attempt <= 12 && !cancelled; attempt += 1) {
			attempts = attempt;
			try {
				await api.deployments.get(params.slug);
				if (!cancelled) {
					ready = true;
					frameLoaded = false;
				}
				return;
			} catch (caught) {
				if (cancelled) return;
				if (caught instanceof ApiError && caught.status !== 503 && caught.status !== 504) {
					error = caught.detail;
					return;
				}
				if (attempt === 12) {
					error = caught instanceof ApiError ? caught.detail : 'Deployment did not wake up in time.';
					return;
				}
				await sleep(1500);
			}
		}
	}

	onMount(() => {
		void wakeDeployment();
	});

	onDestroy(() => {
		cancelled = true;
	});
</script>

<svelte:head>
	<title>{params.slug} | MoLab deployment</title>
</svelte:head>

<div class="fixed inset-0 z-50 bg-slate-950 text-white">
	{#if ready}
		<div class="relative h-full w-full">
			{#if !frameLoaded}
				<div class="absolute inset-0 z-10 grid place-items-center bg-slate-950 px-6 text-center" aria-live="polite">
					<div class="w-full max-w-md rounded-[2rem] border border-white/10 bg-white/10 p-8 shadow-2xl shadow-black/40 backdrop-blur">
						<div class="mx-auto h-3 w-40 overflow-hidden rounded-full bg-white/10">
							<div class="h-full w-1/2 animate-pulse rounded-full bg-orange-300"></div>
						</div>
						<p class="mt-7 text-sm font-semibold uppercase tracking-[0.3em] text-orange-200">Opening app</p>
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
					<p class="mt-4 text-sm leading-6 text-slate-300">{error}</p>
					<button class="mt-7 rounded-full bg-white px-5 py-3 text-sm font-black text-slate-950" type="button" onclick={() => void wakeDeployment()}>
						Try again
					</button>
				{:else}
					<div class="mx-auto h-3 w-40 overflow-hidden rounded-full bg-white/10">
						<div class="h-full w-1/2 animate-pulse rounded-full bg-orange-300"></div>
					</div>
					<p class="mt-7 text-sm font-semibold uppercase tracking-[0.3em] text-orange-200">Waking up...</p>
					<h1 class="mt-5 text-3xl font-black tracking-tight sm:text-5xl">Starting {params.slug}</h1>
					<p class="mt-4 text-sm leading-6 text-slate-300">Cold deployments can take a few seconds before the notebook is ready.</p>
					<p class="mt-5 text-xs font-semibold uppercase tracking-[0.22em] text-slate-500">Attempt {attempts}</p>
				{/if}
			</div>
		</div>
	{/if}
</div>
