<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { ApiError, api } from '$lib/api';
	import { safeNextPath } from '$lib/safeNextPath';
	import { auth } from '$lib/stores/auth';
	import Button from '$lib/components/Button.svelte';

	type LoginErrors = Partial<Record<'username' | 'password' | 'server', string>>;

	let username = $state('');
	let password = $state('');
	let errors = $state<LoginErrors>({});
	let submitting = $state(false);

	const nextParam = $derived(page.url.searchParams.get('next'));
	const nextPath = $derived(safeNextPath(nextParam));
	const registerHref = $derived(nextParam ? `/auth/register?next=${encodeURIComponent(nextParam)}` : '/auth/register');

	function validate() {
		const nextErrors: LoginErrors = {};
		if (!username.trim()) nextErrors.username = 'Enter your username.';
		if (!password) nextErrors.password = 'Enter your password.';
		errors = nextErrors;
		return Object.keys(nextErrors).length === 0;
	}

	async function submit() {
		if (!validate()) return;

		submitting = true;
		errors = {};
		try {
			const token = await api.auth.login({ username: username.trim(), password });
			auth.setSession(token.access_token, { username: username.trim() });
			await goto(nextPath);
		} catch (error) {
			errors = { server: error instanceof ApiError ? error.detail : 'Unable to sign in. Please try again.' };
		} finally {
			submitting = false;
		}
	}
</script>

<svelte:head>
	<title>Login | MarimoHub</title>
</svelte:head>

<section class="mx-auto grid max-w-5xl gap-10 lg:grid-cols-[minmax(0,1fr)_26rem] lg:items-center">
	<div class="space-y-5">
		<p class="w-fit rounded-full bg-hub-50 px-4 py-2 text-sm font-semibold text-hub-950 dark:bg-hub-400/10 dark:text-hub-200">
			Welcome back
		</p>
		<h1 class="text-4xl font-black tracking-tight text-slate-950 dark:text-white sm:text-6xl">Sign in to your notebooks.</h1>
		<p class="max-w-xl text-lg leading-8 text-slate-700 dark:text-slate-300">
			Continue building notebooks, publishing demos, and launching marimo sessions from your workspaces.
		</p>
	</div>

	<form class="rounded-[2rem] border border-slate-900/10 bg-white/80 p-6 shadow-xl shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20" onsubmit={(event) => { event.preventDefault(); void submit(); }} novalidate>
		<div class="space-y-5">
			<div>
				<label class="text-sm font-bold text-slate-800 dark:text-slate-100" for="username">Username</label>
				<input
					class="mt-2 w-full rounded-2xl border border-slate-300 bg-white px-4 py-3 text-slate-950 outline-none transition focus:border-hub-500 focus:ring-4 focus:ring-hub-500/15 dark:border-white/15 dark:bg-slate-950/50 dark:text-white"
					id="username"
					name="username"
					type="text"
					autocomplete="username"
					aria-invalid={Boolean(errors.username)}
					aria-describedby={errors.username ? 'username-error' : undefined}
					bind:value={username}
				/>
				{#if errors.username}
					<p class="mt-2 text-sm font-semibold text-red-700 dark:text-red-300" id="username-error" role="alert">{errors.username}</p>
				{/if}
			</div>

			<div>
				<label class="text-sm font-bold text-slate-800 dark:text-slate-100" for="password">Password</label>
				<input
					class="mt-2 w-full rounded-2xl border border-slate-300 bg-white px-4 py-3 text-slate-950 outline-none transition focus:border-hub-500 focus:ring-4 focus:ring-hub-500/15 dark:border-white/15 dark:bg-slate-950/50 dark:text-white"
					id="password"
					name="password"
					type="password"
					autocomplete="current-password"
					aria-invalid={Boolean(errors.password)}
					aria-describedby={errors.password ? 'password-error' : undefined}
					bind:value={password}
				/>
				{#if errors.password}
					<p class="mt-2 text-sm font-semibold text-red-700 dark:text-red-300" id="password-error" role="alert">{errors.password}</p>
				{/if}
			</div>

			{#if errors.server}
				<p class="rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm font-semibold text-red-800 dark:border-red-400/20 dark:bg-red-500/10 dark:text-red-200" role="alert">{errors.server}</p>
			{/if}

			<Button class="w-full" type="submit" disabled={submitting}>
				{submitting ? 'Signing in...' : 'Sign in'}
			</Button>

			<p class="text-center text-sm text-slate-600 dark:text-slate-300">
				New to MarimoHub?
				<a class="font-bold text-hub-700 hover:text-hub-950 dark:text-hub-300 dark:hover:text-hub-200" href={registerHref}>Create an account</a>
			</p>
		</div>
	</form>
</section>
