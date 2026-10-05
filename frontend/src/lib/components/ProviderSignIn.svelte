<script module lang="ts">
	import type { AuthProvider } from '$lib/api';

	// The provider list only changes when the backend is reconfigured, so moving
	// between sign-in and registration starts from the last answer instead of
	// making the buttons flash in again.
	let lastProviders: AuthProvider[] = [];
</script>

<script lang="ts">
	import { KeyRound } from '@lucide/svelte';
	import { onMount } from 'svelte';
	import { api, oidcLoginUrl } from '$lib/api';
	import { errorBanner } from '$lib/design/classes';
	import {
		OidcUnavailableError,
		providerButtonLabel,
		signInProviders,
		startOidcAttempt,
		type ProviderSignInMode
	} from '$lib/oidc';

	type Props = {
		mode: ProviderSignInMode;
		// Where to land after signing in; must already be a safe path.
		next: string;
	};

	let { mode, next }: Props = $props();

	let providers = $state<AuthProvider[]>(lastProviders);
	let pending = $state<string | null>(null);
	let error = $state<string | null>(null);

	onMount(() => {
		let active = true;
		api.auth.providers().then(
			(value) => {
				lastProviders = signInProviders(value);
				if (active) providers = lastProviders;
			},
			() => {
				// No provider sign-in here (or the backend is unreachable): the
				// username and password form is the whole page.
			}
		);
		return () => {
			active = false;
		};
	});

	async function start(provider: AuthProvider) {
		if (pending) return;
		pending = provider.slug;
		error = null;
		try {
			const challenge = await startOidcAttempt({ provider: provider.slug, next });
			// A full page load, never `goto` or `fetch`: the backend answers with a
			// redirect to the identity provider that the browser itself must follow.
			window.location.assign(oidcLoginUrl(provider.slug, challenge));
		} catch (cause) {
			pending = null;
			error =
				cause instanceof OidcUnavailableError
					? cause.message
					: `Could not start signing in with ${provider.display_name}. Please try again.`;
		}
	}

	// Pressing Back on the identity provider's page can restore this page from
	// the back/forward cache with every button still disabled.
	function onPageShow(event: PageTransitionEvent) {
		if (event.persisted) pending = null;
	}
</script>

<svelte:window onpageshow={onPageShow} />

{#if providers.length > 0}
	<div class="grid gap-3">
		{#if error}
			<p class={errorBanner} role="alert">{error}</p>
		{/if}

		{#each providers as provider (provider.slug)}
			{@const label = providerButtonLabel(provider, mode)}
			{#if provider.kind === 'google'}
				<!--
					Sign in with Google branding: the standard-color "G" on Google's own
					light (#FFFFFF / #747775 / #1F1F1F) or dark (#131314 / #8E918F / #E3E3E3)
					theme, a 20px logo, 12px / 10px / 12px padding, and medium 14/20 text.
					Height and radius follow this form's other controls.
				-->
				<button
					type="button"
					class="gsi-button"
					disabled={pending !== null}
					aria-busy={pending === provider.slug}
					onclick={() => void start(provider)}
				>
					<span class="gsi-state" aria-hidden="true"></span>
					<svg class="gsi-icon" viewBox="12 10 20 20" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" focusable="false">
						<path fill="#4285F4" d="M31.6 20.2273C31.6 19.5182 31.5364 18.8364 31.4182 18.1818H22V22.05H27.3818C27.15 23.3 26.4455 24.3591 25.3864 25.0682V27.5773H28.6182C30.5091 25.8364 31.6 23.2727 31.6 20.2273Z" />
						<path fill="#34A853" d="M22 30C24.7 30 26.9636 29.1045 28.6181 27.5773L25.3863 25.0682C24.4909 25.6682 23.3454 26.0227 22 26.0227C19.3954 26.0227 17.1909 24.2636 16.4045 21.9H13.0636V24.4909C14.7091 27.7591 18.0909 30 22 30Z" />
						<path fill="#FBBC04" d="M16.4045 21.9C16.2045 21.3 16.0909 20.6591 16.0909 20C16.0909 19.3409 16.2045 18.7 16.4045 18.1V15.5091H13.0636C12.3864 16.8591 12 18.3864 12 20C12 21.6136 12.3864 23.1409 13.0636 24.4909L16.4045 21.9Z" />
						<path fill="#E94235" d="M22 13.9773C23.4681 13.9773 24.7863 14.4818 25.8227 15.4727L28.6909 12.6045C26.9591 10.9909 24.6954 10 22 10C18.0909 10 14.7091 12.2409 13.0636 15.5091L16.4045 18.1C17.1909 15.7364 19.3954 13.9773 22 13.9773Z" />
					</svg>
					<span class="gsi-label">{label}</span>
					<span class="gsi-spacer" aria-hidden="true"></span>
				</button>
			{:else}
				<button
					type="button"
					class="relative flex h-11 w-full items-center rounded-md border border-app-line bg-app-card px-3 text-sm font-semibold text-app-fg shadow-[var(--shadow-sm)] transition hover:border-app-line-strong hover:bg-app-sidebar-hover disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:border-app-line disabled:hover:bg-app-card"
					disabled={pending !== null}
					aria-busy={pending === provider.slug}
					onclick={() => void start(provider)}
				>
					<span class="mr-2.5 grid size-5 shrink-0 place-items-center text-brand-strong" aria-hidden="true"><KeyRound size={18} /></span>
					<span class="min-w-0 flex-1 truncate text-center">{label}</span>
					<span class="ml-2.5 size-5 shrink-0" aria-hidden="true"></span>
				</button>
			{/if}
		{/each}
	</div>

	<div class="my-6 flex items-center gap-3 text-xs font-medium text-app-muted">
		<span class="h-px flex-1 bg-app-line" aria-hidden="true"></span>
		or
		<span class="h-px flex-1 bg-app-line" aria-hidden="true"></span>
	</div>
{/if}

<style>
	.gsi-button {
		position: relative;
		display: flex;
		align-items: center;
		width: 100%;
		height: 2.75rem;
		padding: 0 12px;
		overflow: hidden;
		border: 1px solid #747775;
		border-radius: var(--radius-sm);
		background-color: #ffffff;
		color: #1f1f1f;
		font-family: 'Google Sans', Roboto, Arial, sans-serif;
		font-size: 14px;
		font-weight: 500;
		line-height: 20px;
		letter-spacing: 0.25px;
		white-space: nowrap;
		cursor: pointer;
		transition:
			background-color 0.218s,
			border-color 0.218s,
			box-shadow 0.218s;
	}

	:global(.dark) .gsi-button {
		border-color: #8e918f;
		background-color: #131314;
		color: #e3e3e3;
	}

	.gsi-state {
		position: absolute;
		inset: 0;
		background-color: #303030;
		opacity: 0;
		transition: opacity 0.218s;
	}

	:global(.dark) .gsi-state {
		background-color: #e3e3e3;
	}

	.gsi-icon {
		position: relative;
		flex: none;
		width: 20px;
		height: 20px;
		margin-right: 10px;
	}

	.gsi-label {
		position: relative;
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-align: center;
		text-overflow: ellipsis;
	}

	/* Mirrors the logo so the label is centered on the whole button. */
	.gsi-spacer {
		flex: none;
		width: 20px;
		margin-left: 10px;
	}

	.gsi-button:not(:disabled):hover {
		box-shadow:
			0 1px 2px 0 rgb(60 64 67 / 0.3),
			0 1px 3px 1px rgb(60 64 67 / 0.15);
	}

	.gsi-button:not(:disabled):hover .gsi-state {
		opacity: 0.08;
	}

	.gsi-button:not(:disabled):active .gsi-state,
	.gsi-button:not(:disabled):focus-visible .gsi-state {
		opacity: 0.12;
	}

	.gsi-button:disabled {
		cursor: not-allowed;
		border-color: #1f1f1f1f;
		background-color: #ffffff61;
	}

	:global(.dark) .gsi-button:disabled {
		border-color: #8e918f1f;
		background-color: #13131461;
	}

	.gsi-button:disabled .gsi-icon,
	.gsi-button:disabled .gsi-label {
		opacity: 0.38;
	}
</style>
