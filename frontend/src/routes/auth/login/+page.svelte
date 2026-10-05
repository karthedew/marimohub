<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { ApiError, api } from '$lib/api';
	import { consumeOidcAttempt, oidcErrorMessage } from '$lib/oidc';
	import { safeNextPath } from '$lib/safeNextPath';
	import { auth } from '$lib/stores/auth';
	import { ArrowRight, LockKeyhole, UserRound } from '@lucide/svelte';
	import Button from '$lib/components/Button.svelte';
	import ProviderSignIn from '$lib/components/ProviderSignIn.svelte';
	import { errorBanner, fieldError, fieldLabel } from '$lib/design/classes';
	import AuthShell from '$lib/design/components/AuthShell.svelte';

	const authInput =
		'h-11 w-full rounded-md border border-app-line bg-app-card pl-10 pr-3 text-sm text-app-fg outline-none transition placeholder:text-app-muted focus:border-brand-strong aria-[invalid=true]:border-app-danger';

	type LoginErrors = Partial<Record<'username' | 'password' | 'server', string>>;

	let username = $state('');
	let password = $state('');
	let errors = $state<LoginErrors>({});
	let submitting = $state(false);
	let recoveredNext = $state<string | null>(null);
	let providerErrorDismissed = $state(false);

	const nextParam = $derived(page.url.searchParams.get('next') ?? recoveredNext);
	const nextPath = $derived(safeNextPath(nextParam));
	const registerHref = $derived(nextParam ? `/auth/register?next=${encodeURIComponent(nextParam)}` : '/auth/register');
	// A provider sign-in that failed comes back here as `?error=<code>`.
	const providerError = $derived(providerErrorDismissed ? null : oidcErrorMessage(page.url.searchParams.get('error')));

	// That failed attempt's record is dead either way, so it is cleared here —
	// and the `next` it started with, which the backend's redirect cannot carry,
	// is picked back up so signing in another way still lands there.
	onMount(() => {
		if (!page.url.searchParams.has('error')) return;
		const attempt = consumeOidcAttempt();
		const attemptNext = attempt ? safeNextPath(attempt.next) : '/';
		if (attemptNext !== '/' && !page.url.searchParams.get('next')) recoveredNext = attemptNext;
	});

	function validate() {
		const nextErrors: LoginErrors = {};
		if (!username.trim()) nextErrors.username = 'Enter your username.';
		if (!password) nextErrors.password = 'Enter your password.';
		errors = nextErrors;
		return Object.keys(nextErrors).length === 0;
	}

	async function submit() {
		providerErrorDismissed = true;
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

<AuthShell
	eyebrow="Welcome back"
	title="Sign in to your notebooks."
	detail="Continue building notebooks, publishing demos, and launching marimo sessions from your workspaces."
>
	{#if providerError}
		<p class="{errorBanner} mb-5" role="alert">{providerError}</p>
	{/if}

	<ProviderSignIn mode="login" next={nextPath} />

	<form class="grid gap-4" onsubmit={(event) => { event.preventDefault(); void submit(); }} novalidate>
		{#if errors.server}
			<p class={errorBanner} role="alert">{errors.server}</p>
		{/if}

		<div class="grid gap-1.5">
			<label class={fieldLabel} for="username">Username</label>
			<span class="relative block">
				<UserRound size={16} class="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-app-muted" />
				<input
					class={authInput}
					id="username"
					name="username"
					type="text"
					autocomplete="username"
					placeholder="your-username"
					aria-invalid={Boolean(errors.username)}
					aria-describedby={errors.username ? 'username-error' : undefined}
					bind:value={username}
				/>
			</span>
			{#if errors.username}
				<p class={fieldError} id="username-error" role="alert">{errors.username}</p>
			{/if}
		</div>

		<div class="grid gap-1.5">
			<label class={fieldLabel} for="password">Password</label>
			<span class="relative block">
				<LockKeyhole size={16} class="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-app-muted" />
				<input
					class={authInput}
					id="password"
					name="password"
					type="password"
					autocomplete="current-password"
					placeholder="Enter your password"
					aria-invalid={Boolean(errors.password)}
					aria-describedby={errors.password ? 'password-error' : undefined}
					bind:value={password}
				/>
			</span>
			{#if errors.password}
				<p class={fieldError} id="password-error" role="alert">{errors.password}</p>
			{/if}
		</div>

		<Button class="mt-1 w-full" size="lg" type="submit" disabled={submitting}>
			{submitting ? 'Signing in...' : 'Sign in'}
			{#if !submitting}<ArrowRight size={16} />{/if}
		</Button>
	</form>

	<p class="mt-6 text-center text-sm text-app-muted">
		New to MarimoHub?
		<a class="font-semibold text-brand-strong hover:underline" href={registerHref}>Create an account</a>
	</p>
</AuthShell>
