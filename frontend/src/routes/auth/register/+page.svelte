<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { ApiError, api } from '$lib/api';
	import { safeNextPath } from '$lib/safeNextPath';
	import { auth } from '$lib/stores/auth';
	import { ArrowRight, Contact, LockKeyhole, Mail, UserRound } from '@lucide/svelte';
	import Button from '$lib/components/Button.svelte';
	import ProviderSignIn from '$lib/components/ProviderSignIn.svelte';
	import { errorBanner, fieldError, fieldHint, fieldLabel } from '$lib/design/classes';
	import AuthShell from '$lib/design/components/AuthShell.svelte';

	const authInput =
		'h-11 w-full rounded-md border border-app-line bg-app-card pl-10 pr-3 text-sm text-app-fg outline-none transition placeholder:text-app-muted focus:border-brand-strong aria-[invalid=true]:border-app-danger';

	type RegisterErrors = Partial<Record<'username' | 'email' | 'password' | 'server', string>>;

	let displayName = $state('');
	let username = $state('');
	let email = $state('');
	let password = $state('');
	let errors = $state<RegisterErrors>({});
	let submitting = $state(false);

	const nextParam = $derived(page.url.searchParams.get('next'));
	const nextPath = $derived(safeNextPath(nextParam));
	const loginHref = $derived(nextParam ? `/auth/login?next=${encodeURIComponent(nextParam)}` : '/auth/login');

	function validate() {
		const nextErrors: RegisterErrors = {};
		const trimmedEmail = email.trim();

		if (!username.trim()) nextErrors.username = 'Choose a username.';
		if (!trimmedEmail) nextErrors.email = 'Enter your email address.';
		else if (!/^\S+@\S+\.\S+$/.test(trimmedEmail)) nextErrors.email = 'Enter a valid email address.';
		if (password.length < 8) nextErrors.password = 'Use at least 8 characters.';

		errors = nextErrors;
		return Object.keys(nextErrors).length === 0;
	}

	async function submit() {
		if (!validate()) return;

		submitting = true;
		errors = {};
		// Optional: a blank name is simply not sent, which the backend reads as none.
		const fullName = displayName.trim();
		try {
			const user = await api.auth.register({
				username: username.trim(),
				email: email.trim(),
				password,
				...(fullName ? { display_name: fullName } : {})
			});
			const token = await api.auth.login({ username: username.trim(), password });
			auth.setSession(token.access_token, user);
			await goto(nextPath);
		} catch (error) {
			errors = { server: error instanceof ApiError ? error.detail : 'Unable to create your account. Please try again.' };
		} finally {
			submitting = false;
		}
	}
</script>

<svelte:head>
	<title>Register | MarimoHub</title>
</svelte:head>

<AuthShell
	eyebrow="Start building"
	title="Create your MarimoHub account."
	detail="Create private notebooks, collaborate inside a workspace, and prepare your marimo work for sharing."
>
	<ProviderSignIn mode="register" next={nextPath} />

	<form class="grid gap-4" onsubmit={(event) => { event.preventDefault(); void submit(); }} novalidate>
		{#if errors.server}
			<p class={errorBanner} role="alert">{errors.server}</p>
		{/if}

		<div class="grid gap-1.5">
			<label class={fieldLabel} for="display-name">Full name</label>
			<span class="relative block">
				<Contact size={16} class="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-app-muted" />
				<input
					class={authInput}
					id="display-name"
					name="display_name"
					type="text"
					autocomplete="name"
					maxlength={255}
					placeholder="Ada Lovelace"
					aria-describedby="display-name-hint"
					bind:value={displayName}
				/>
			</span>
			<p class={fieldHint} id="display-name-hint">Optional. Workspace Owners can find you by this name.</p>
		</div>

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
			<label class={fieldLabel} for="email">Email</label>
			<span class="relative block">
				<Mail size={16} class="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-app-muted" />
				<input
					class={authInput}
					id="email"
					name="email"
					type="email"
					autocomplete="email"
					placeholder="you@example.org"
					aria-invalid={Boolean(errors.email)}
					aria-describedby={errors.email ? 'email-error' : undefined}
					bind:value={email}
				/>
			</span>
			{#if errors.email}
				<p class={fieldError} id="email-error" role="alert">{errors.email}</p>
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
					autocomplete="new-password"
					placeholder="At least 8 characters"
					aria-invalid={Boolean(errors.password)}
					aria-describedby={errors.password ? 'password-error' : 'password-hint'}
					bind:value={password}
				/>
			</span>
			{#if errors.password}
				<p class={fieldError} id="password-error" role="alert">{errors.password}</p>
			{:else}
				<p class={fieldHint} id="password-hint">Use at least 8 characters.</p>
			{/if}
		</div>

		<Button class="mt-1 w-full" size="lg" type="submit" disabled={submitting}>
			{submitting ? 'Creating account...' : 'Create account'}
			{#if !submitting}<ArrowRight size={16} />{/if}
		</Button>
	</form>

	<p class="mt-6 text-center text-sm text-app-muted">
		Already have an account?
		<a class="font-semibold text-brand-strong hover:underline" href={loginHref}>Sign in</a>
	</p>
</AuthShell>
