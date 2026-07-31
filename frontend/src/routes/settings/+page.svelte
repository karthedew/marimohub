<script lang="ts">
	import { auth } from '$lib/stores/auth';
	import { profileAvatars } from '$lib/stores/profile';
	import Button from '$lib/components/Button.svelte';

	let error = $state<string | null>(null);
	let copied = $state(false);

	const user = $derived($auth.currentUser);
	const avatar = $derived(user ? $profileAvatars[user.id] : undefined);
	const initial = $derived(user?.username.slice(0, 1).toUpperCase() ?? 'U');

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
	<section class="mx-auto max-w-3xl space-y-7">
		<div>
			<p class="text-sm font-semibold text-hub-700 dark:text-hub-300">Settings</p>
			<h1 class="mt-1 text-3xl font-black tracking-tight text-slate-950 dark:text-white sm:text-4xl">Profile</h1>
			<p class="mt-2 text-sm text-slate-500 dark:text-slate-400">Manage how you appear in this browser.</p>
		</div>

		<div class="rounded-2xl border border-slate-900/10 bg-white p-6 shadow-sm dark:border-white/10 dark:bg-[#181a1b] sm:p-8">
			<div class="flex flex-col gap-5 sm:flex-row sm:items-center">
				<div class="grid size-20 shrink-0 place-items-center overflow-hidden rounded-full bg-slate-900 text-2xl font-black text-white dark:bg-slate-100 dark:text-slate-950">
					{#if avatar}<img class="size-full object-cover" src={avatar} alt="Your profile" />{:else}{initial}{/if}
				</div>
				<div>
					<p class="font-bold text-slate-950 dark:text-white">Profile picture</p>
					<p class="mt-1 text-sm text-slate-500 dark:text-slate-400">JPG, PNG, or WebP. Maximum 2 MB.</p>
					<div class="mt-3 flex flex-wrap gap-2">
						<label class="inline-flex cursor-pointer items-center justify-center rounded-full bg-hub-700 px-4 py-2 text-sm font-bold text-white transition hover:bg-hub-800 focus-within:ring-4 focus-within:ring-hub-600/20">
							Choose image
							<input class="sr-only" type="file" accept="image/*" onchange={(event) => void updateAvatar(event)} />
						</label>
						{#if avatar}<Button type="button" intent="secondary" size="sm" onclick={() => profileAvatars.update(user.id, null)}>Remove</Button>{/if}
					</div>
				</div>
			</div>
			{#if error}<p class="mt-4 text-sm font-semibold text-red-700 dark:text-red-300" role="alert">{error}</p>{/if}
		</div>

		<div class="rounded-2xl border border-slate-900/10 bg-white p-6 shadow-sm dark:border-white/10 dark:bg-[#181a1b] sm:p-8">
			<h2 class="text-lg font-bold text-slate-950 dark:text-white">Identity</h2>
			<dl class="mt-5 divide-y divide-slate-900/10 text-sm dark:divide-white/10">
				<div class="grid gap-1 py-4 first:pt-0 sm:grid-cols-[10rem_1fr]"><dt class="font-semibold text-slate-500 dark:text-slate-400">Username</dt><dd class="font-medium text-slate-950 dark:text-white">{user.username}</dd></div>
				{#if user.email}<div class="grid gap-1 py-4 sm:grid-cols-[10rem_1fr]"><dt class="font-semibold text-slate-500 dark:text-slate-400">Email</dt><dd class="font-medium text-slate-950 dark:text-white">{user.email}</dd></div>{/if}
				<div class="grid gap-2 py-4 last:pb-0 sm:grid-cols-[10rem_1fr]"><dt class="font-semibold text-slate-500 dark:text-slate-400">User ID</dt><dd class="flex min-w-0 flex-wrap items-center gap-2"><code class="min-w-0 break-all rounded-lg bg-slate-100 px-2 py-1 text-xs text-slate-700 dark:bg-slate-800 dark:text-slate-300">{user.id}</code><Button type="button" intent="secondary" size="sm" onclick={() => void copyUserId()}>{copied ? 'Copied' : 'Copy'}</Button></dd></div>
			</dl>
		</div>

		<p class="text-xs leading-5 text-slate-400">Profile pictures are stored in this browser. MarimoHub does not yet sync profile changes between devices.</p>
	</section>
{/if}
