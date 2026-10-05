<script lang="ts">
	import { auth } from '$lib/stores/auth';
	import { profileAvatars } from '$lib/stores/profile';
	import { Check, Copy, ImageUp, Info, Trash, UserRound } from '@lucide/svelte';
	import Button from '$lib/components/Button.svelte';
	import { card, cardHeader, cardTitle, fieldError } from '$lib/design/classes';

	let error = $state<string | null>(null);
	let copied = $state(false);

	const user = $derived($auth.currentUser);
	const avatar = $derived(user ? $profileAvatars[user.id] : undefined);
	const initial = $derived(user?.username.slice(0, 2).toUpperCase() ?? 'U');

	function readDataUrl(file: File) {
		return new Promise<string>((resolve, reject) => {
			const reader = new FileReader();
			reader.onload = () => typeof reader.result === 'string' ? resolve(reader.result) : reject(new Error('Unable to read image.'));
			reader.onerror = () => reject(reader.error ?? new Error('Unable to read image.'));
			reader.readAsDataURL(file);
		});
	}

	async function updateAvatar(event: Event) {
		const file = (event.currentTarget as HTMLInputElement).files?.[0];
		if (!file || !user) return;
		error = null;
		if (!file.type.startsWith('image/')) {
			error = 'Choose an image file.';
			return;
		}
		if (file.size > 2_000_000) {
			error = 'Choose an image smaller than 2 MB.';
			return;
		}
		try {
			profileAvatars.update(user.id, await readDataUrl(file));
		} catch {
			error = 'Unable to read that image.';
		}
	}

	async function copyUserId() {
		if (!user) return;
		await navigator.clipboard.writeText(user.id);
		copied = true;
		setTimeout(() => (copied = false), 2000);
	}
</script>

<svelte:head><title>Profile settings | MarimoHub</title></svelte:head>

{#if user}
	<div class="mb-6 flex items-center gap-3">
		<span class="grid size-11 shrink-0 place-items-center overflow-hidden rounded-full bg-[#26333b] text-sm font-semibold text-white">
			{#if avatar}<img class="size-full object-cover" src={avatar} alt="" />{:else}{initial}{/if}
		</span>
		<div class="min-w-0">
			<p class="text-xs font-medium text-app-muted">Settings</p>
			<h1>Profile</h1>
		</div>
	</div>
	<p class="-mt-3 mb-6 text-sm text-app-muted">Manage how you appear in this browser.</p>

	<div class="grid gap-5 xl:grid-cols-[minmax(0,1fr)_320px] xl:items-start">
		<div class="grid min-w-0 gap-5">
			<section class={card} aria-labelledby="picture-heading">
				<div class="{cardHeader} flex items-center gap-3">
					<span class="grid size-9 place-items-center rounded-md bg-brand-soft text-brand-strong"><UserRound size={18} /></span>
					<div>
						<h2 id="picture-heading" class={cardTitle}>Profile picture</h2>
						<p class="mt-0.5 text-xs text-app-muted">JPG, PNG, or WebP. Maximum 2 MB.</p>
					</div>
				</div>
				<div class="flex flex-col gap-5 p-5 sm:flex-row sm:items-center">
					<div class="grid size-20 shrink-0 place-items-center overflow-hidden rounded-full bg-[#26333b] text-2xl font-semibold text-white">
						{#if avatar}<img class="size-full object-cover" src={avatar} alt="Your profile" />{:else}{initial}{/if}
					</div>
					<div class="flex flex-wrap gap-2">
						<label class="inline-flex h-9 cursor-pointer items-center justify-center gap-2 rounded-md bg-brand px-3.5 text-sm font-semibold text-on-brand transition hover:brightness-95 focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-[var(--focus)]">
							<ImageUp size={15} />Choose image
							<input class="sr-only" type="file" accept="image/*" onchange={(event) => void updateAvatar(event)} />
						</label>
						{#if avatar}<Button type="button" intent="secondary" onclick={() => profileAvatars.update(user.id, null)}><Trash size={14} />Remove</Button>{/if}
					</div>
				</div>
				{#if error}<p class="{fieldError} px-5 pb-5" role="alert">{error}</p>{/if}
			</section>

			<section class={card} aria-labelledby="identity-heading">
				<div class={cardHeader}>
					<h2 id="identity-heading" class={cardTitle}>Identity</h2>
					<p class="mt-0.5 text-xs text-app-muted">What MarimoHub knows about this signed-in browser session.</p>
				</div>
				<dl class="m-0 divide-y divide-app-line text-sm">
					<div class="grid gap-1 px-5 py-3.5 sm:grid-cols-[10rem_1fr] sm:items-center"><dt class="text-xs font-medium text-app-muted">Username</dt><dd class="m-0 font-medium">{user.username}</dd></div>
					{#if user.email}<div class="grid gap-1 px-5 py-3.5 sm:grid-cols-[10rem_1fr] sm:items-center"><dt class="text-xs font-medium text-app-muted">Email</dt><dd class="m-0 font-medium">{user.email}</dd></div>{/if}
					<div class="grid gap-2 px-5 py-3.5 sm:grid-cols-[10rem_1fr] sm:items-center">
						<dt class="text-xs font-medium text-app-muted">User ID</dt>
						<dd class="m-0 flex min-w-0 flex-wrap items-center gap-2">
							<code class="min-w-0 break-all rounded-md border border-app-line bg-app-bg px-2 py-1 text-xs">{user.id}</code>
							<Button type="button" intent="secondary" size="sm" onclick={() => void copyUserId()}>
								{#if copied}<Check size={14} />{:else}<Copy size={14} />{/if}{copied ? 'Copied' : 'Copy'}
							</Button>
						</dd>
					</div>
				</dl>
			</section>
		</div>

		<aside class="{card} p-5" aria-label="About profile storage">
			<div class="flex items-center gap-2">
				<Info size={16} class="text-brand-strong" />
				<h2 class="m-0 border-0 p-0 text-sm font-semibold">Stored in this browser</h2>
			</div>
			<p class="mt-3 text-xs leading-5 text-app-muted">Profile pictures are stored in this browser. MarimoHub does not yet sync profile changes between devices.</p>
			<p class="mt-3 border-t border-app-line pt-3 text-xs leading-5 text-app-muted">
				Share your User ID with a Workspace Owner so they can add you as a member.
			</p>
		</aside>
	</div>
{/if}
