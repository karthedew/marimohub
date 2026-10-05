<script lang="ts">
	import { ArrowLeft, ArrowRight, LoaderCircle } from '@lucide/svelte';
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { api } from '$lib/api';
	import Button from '$lib/components/Button.svelte';
	import { errorBanner } from '$lib/design/classes';
	import AuthShell from '$lib/design/components/AuthShell.svelte';
	import { consumeOidcAttempt } from '$lib/oidc';
	import { completeOidcCallback, oidcCallbackMessage, type OidcCallbackResult } from '$lib/oidcCallback';
	import { auth } from '$lib/stores/auth';

	// Where the backend sends the browser after a provider sign-in:
	// `/auth/callback#handoff=<token>`. The steps live in `$lib/oidcCallback`.
	let result = $state<OidcCallbackResult | null>(null);

	const title = $derived(
		!result ? 'Signing you in…' : result.ok ? 'You are signed in.' : 'We couldn’t sign you in.'
	);
	const detail = $derived(
		!result
			? 'Finishing sign-in with your identity provider.'
			: result.ok
				? 'Continue to where you were going.'
				: 'Nothing was changed. You can start again from the sign-in page.'
	);
	const loginHref = $derived(
		result && !result.ok && result.next && result.next !== '/'
			? `/auth/login?next=${encodeURIComponent(result.next)}`
			: '/auth/login'
	);

	onMount(() => {
		void completeOidcCallback({
			hash: window.location.hash,
			// The native call on purpose: SvelteKit's own `replaceState` would copy
			// the current URL — handoff included — into the history entry's state.
			scrubUrl: () => history.replaceState(history.state, '', `${location.pathname}${location.search}`),
			consumeAttempt: () => consumeOidcAttempt(),
			exchange: (body) => api.auth.oidcExchange(body),
			setSession: (token, user) => auth.setSession(token, user),
			navigate: (path) => goto(path, { replaceState: true })
		})
			.then((outcome) => {
				result = outcome;
			})
			// Anything unforeseen still ends on an error with a way back, never on the spinner.
			.catch(() => {
				result = { ok: false, failure: 'unavailable', next: null };
			});
	});
</script>

<svelte:head>
	<title>Signing in | MarimoHub</title>
	<meta name="referrer" content="no-referrer" />
</svelte:head>

<AuthShell eyebrow="Single sign-on" {title} {detail}>
	{#if !result}
		<p class="flex items-center gap-2.5 text-sm text-app-muted" role="status">
			<LoaderCircle size={16} class="animate-spin text-brand-strong" aria-hidden="true" />
			Just a moment…
		</p>
	{:else if result.ok}
		<Button class="w-full" size="lg" href={result.next}>Continue <ArrowRight size={16} /></Button>
	{:else}
		<p class={errorBanner} role="alert">{oidcCallbackMessage(result.failure)}</p>
		<Button class="mt-5 w-full" size="lg" href={loginHref}><ArrowLeft size={16} />Back to sign in</Button>
	{/if}
</AuthShell>
