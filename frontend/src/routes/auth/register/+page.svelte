<script lang="ts">
	import { goto } from '$app/navigation';
	import { ApiError, api } from '$lib/api';
	import { auth } from '$lib/stores/auth';

	type RegisterErrors = Partial<Record<'username' | 'email' | 'password' | 'server', string>>;

	let username = $state('');
	let email = $state('');
	let password = $state('');
	let errors = $state<RegisterErrors>({});
	let submitting = $state(false);

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
		try {
			const user = await api.auth.register({ username: username.trim(), email: email.trim(), password });
			const token = await api.auth.login({ username: username.trim(), password });
			auth.setSession(token.access_token, user);
			await goto('/');
		} catch (error) {
			errors = { server: error instanceof ApiError ? error.detail : 'Unable to create your account. Please try again.' };
		} finally {
			submitting = false;
		}
	}
</script>

<svelte:head>
	<title>Register | MoLab</title>
</svelte:head>

<section class="mx-auto grid max-w-5xl gap-10 lg:grid-cols-[minmax(0,1fr)_26rem] lg:items-center">
	<div class="space-y-5">
		<p class="w-fit rounded-full bg-orange-100 px-4 py-2 text-sm font-semibold text-orange-900 dark:bg-orange-400/10 dark:text-orange-200">
			Start building
		</p>
		<h1 class="text-4xl font-black tracking-tight text-slate-950 dark:text-white sm:text-6xl">Create your MoLab account.</h1>
		<p class="max-w-xl text-lg leading-8 text-slate-700 dark:text-slate-300">
			Save private drafts, publish notebooks, and prepare your marimo work for sharing.
		</p>
	</div>

	<form class="rounded-[2rem] border border-slate-900/10 bg-white/80 p-6 shadow-xl shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20" onsubmit={(event) => { event.preventDefault(); void submit(); }} novalidate>
		<div class="space-y-5">
			<div>
				<label class="text-sm font-bold text-slate-800 dark:text-slate-100" for="username">Username</label>
				<input
					class="mt-2 w-full rounded-2xl border border-slate-300 bg-white px-4 py-3 text-slate-950 outline-none transition focus:border-orange-500 focus:ring-4 focus:ring-orange-500/15 dark:border-white/15 dark:bg-slate-950/50 dark:text-white"
					id="username"
					name="username"
					type="text"
					autocomplete="username"
					aria-invalid={Boolean(errors.username)}
					aria-describedby={errors.username ? 'username-error' : undefined}
					bind:value={username}
				/>
				{#if errors.username}
					<p class="mt-2 text-sm font-semibold text-red-700 dark:text-red-300" id="username-error">{errors.username}</p>
				{/if}
			</div>

			<div>
				<label class="text-sm font-bold text-slate-800 dark:text-slate-100" for="email">Email</label>
				<input
					class="mt-2 w-full rounded-2xl border border-slate-300 bg-white px-4 py-3 text-slate-950 outline-none transition focus:border-orange-500 focus:ring-4 focus:ring-orange-500/15 dark:border-white/15 dark:bg-slate-950/50 dark:text-white"
					id="email"
					name="email"
					type="email"
					autocomplete="email"
					aria-invalid={Boolean(errors.email)}
					aria-describedby={errors.email ? 'email-error' : undefined}
					bind:value={email}
				/>
				{#if errors.email}
					<p class="mt-2 text-sm font-semibold text-red-700 dark:text-red-300" id="email-error">{errors.email}</p>
				{/if}
			</div>

			<div>
				<label class="text-sm font-bold text-slate-800 dark:text-slate-100" for="password">Password</label>
				<input
					class="mt-2 w-full rounded-2xl border border-slate-300 bg-white px-4 py-3 text-slate-950 outline-none transition focus:border-orange-500 focus:ring-4 focus:ring-orange-500/15 dark:border-white/15 dark:bg-slate-950/50 dark:text-white"
					id="password"
					name="password"
					type="password"
					autocomplete="new-password"
					aria-invalid={Boolean(errors.password)}
					aria-describedby={errors.password ? 'password-error' : undefined}
					bind:value={password}
				/>
				{#if errors.password}
					<p class="mt-2 text-sm font-semibold text-red-700 dark:text-red-300" id="password-error">{errors.password}</p>
				{/if}
			</div>

			{#if errors.server}
				<p class="rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm font-semibold text-red-800 dark:border-red-400/20 dark:bg-red-500/10 dark:text-red-200">{errors.server}</p>
			{/if}

			<button class="w-full rounded-full bg-graphite px-5 py-3 text-sm font-bold text-white shadow-lg shadow-slate-950/10 disabled:cursor-not-allowed disabled:opacity-60 dark:bg-white dark:text-slate-950" type="submit" disabled={submitting}>
				{submitting ? 'Creating account...' : 'Create account'}
			</button>

			<p class="text-center text-sm text-slate-600 dark:text-slate-300">
				Already have an account?
				<a class="font-bold text-orange-700 hover:text-orange-900 dark:text-orange-300 dark:hover:text-orange-200" href="/auth/login">Sign in</a>
			</p>
		</div>
	</form>
</section>
