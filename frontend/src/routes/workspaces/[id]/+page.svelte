<script lang="ts">
	import { goto, invalidateAll } from '$app/navigation';
	import { ApiError, api, type WorkspaceMember, type WorkspaceRole } from '$lib/api';
	import { workspaces } from '$lib/stores/workspaces';
	import Button from '$lib/components/Button.svelte';

	let { data } = $props();

	// Derived, not locally-owned state: this page has no independent source of
	// truth for the workspace or its members, so every mutation below reloads
	// `data` through `invalidateAll()` rather than hand-patching a local copy
	// that could drift from what the backend actually persisted.
	const isOwner = $derived(data.workspace.role === 'owner');
	const canCreateNotebooks = $derived(data.workspace.role !== 'viewer');
	const name = $derived(data.workspace.name);
	const members = $derived(data.members);

	let renaming = $state(false);
	let renameValue = $state('');
	let renameError = $state<string | null>(null);
	let renameSubmitting = $state(false);

	let newMemberId = $state('');
	let newMemberRole = $state<WorkspaceRole>('editor');
	let addMemberError = $state<string | null>(null);
	let addingMember = $state(false);

	let rowError = $state<{ userId: string; message: string } | null>(null);
	let rowBusy = $state<string | null>(null);

	let archiving = $state(false);
	let confirmingArchive = $state(false);
	let archiveError = $state<string | null>(null);

	// If a focus refresh finds this workspace missing from the caller's own
	// list — another owner removed them, or the workspace was archived — this
	// page can no longer show accurate data, so it sends the caller back
	// rather than let them keep acting on a stale view.
	$effect(() => {
		if ($workspaces.status !== 'ready') return;
		if ($workspaces.items.some((item) => item.id === data.workspace.id)) return;
		void goto('/workspaces?notice=workspace-unavailable', { replaceState: true });
	});

	function formatDate(value: string) {
		return new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric', year: 'numeric' }).format(new Date(value));
	}

	function startRename() {
		renameValue = name;
		renameError = null;
		renaming = true;
	}

	async function submitRename() {
		if (!renameValue.trim()) {
			renameError = 'Enter a workspace name.';
			return;
		}
		renameSubmitting = true;
		renameError = null;
		try {
			await api.workspaces.rename(data.workspace.id, { name: renameValue.trim() });
			renaming = false;
			await Promise.all([invalidateAll(), workspaces.refresh()]);
		} catch (caught) {
			renameError = caught instanceof ApiError ? caught.detail : 'Unable to rename workspace.';
		} finally {
			renameSubmitting = false;
		}
	}

	async function addMember() {
		addMemberError = null;
		if (!newMemberId.trim()) {
			addMemberError = 'Enter a User ID.';
			return;
		}

		addingMember = true;
		try {
			await api.workspaces.members.add(data.workspace.id, {
				user_id: newMemberId.trim(),
				role: newMemberRole
			});
			newMemberId = '';
			newMemberRole = 'editor';
			await Promise.all([invalidateAll(), workspaces.refresh()]);
		} catch (caught) {
			addMemberError = caught instanceof ApiError ? caught.detail : 'Unable to add member.';
		} finally {
			addingMember = false;
		}
	}

	async function changeRole(member: WorkspaceMember, role: WorkspaceRole) {
		if (role === member.role) return;
		rowError = null;
		rowBusy = member.user_id;
		try {
			await api.workspaces.members.updateRole(data.workspace.id, member.user_id, { role });
			await Promise.all([invalidateAll(), workspaces.refresh()]);
		} catch (caught) {
			rowError = {
				userId: member.user_id,
				message: caught instanceof ApiError ? caught.detail : 'Unable to change role.'
			};
		} finally {
			rowBusy = null;
		}
	}

	async function removeMember(member: WorkspaceMember) {
		rowError = null;
		rowBusy = member.user_id;
		try {
			await api.workspaces.members.remove(data.workspace.id, member.user_id);
			await Promise.all([invalidateAll(), workspaces.refresh()]);
		} catch (caught) {
			rowError = {
				userId: member.user_id,
				message: caught instanceof ApiError ? caught.detail : 'Unable to remove member.'
			};
		} finally {
			rowBusy = null;
		}
	}

	async function archiveWorkspace() {
		archiving = true;
		archiveError = null;
		try {
			await api.workspaces.archive(data.workspace.id);
			await workspaces.refresh();
			await goto('/workspaces?notice=workspace-archived');
		} catch (caught) {
			archiveError = caught instanceof ApiError ? caught.detail : 'Unable to archive workspace.';
			archiving = false;
		}
	}
</script>

<svelte:head>
	<title>{name} | MarimoHub</title>
</svelte:head>

<section class="space-y-8">
	<div class="flex flex-wrap items-center justify-between gap-3">
		<Button intent="secondary" size="sm" class="w-fit" href="/workspaces">Back to workspaces</Button>
		{#if canCreateNotebooks}
			<Button intent="primary" size="sm" href={`/notebooks/new?workspace=${data.workspace.id}`}>New Notebook</Button>
		{/if}
	</div>

	<div class="rounded-[2rem] border border-slate-900/10 bg-white/80 p-7 shadow-xl shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20 sm:p-9">
		<div class="flex flex-wrap items-center gap-3">
			<span class="rounded-full bg-hub-50 px-4 py-2 text-sm font-semibold capitalize text-hub-950 dark:bg-hub-400/10 dark:text-hub-200">{data.workspace.role}</span>
			<span class="rounded-full bg-slate-100 px-4 py-2 font-mono text-sm font-semibold text-slate-700 dark:bg-white/10 dark:text-slate-200">{data.workspace.slug}</span>
		</div>

		{#if renaming}
			<form class="mt-6 flex flex-wrap items-end gap-3" onsubmit={(event) => { event.preventDefault(); void submitRename(); }}>
				<label class="grid gap-2 text-sm font-semibold text-slate-700 dark:text-slate-200">
					Name
					<input
						class="rounded-2xl border border-slate-300/80 bg-white/80 px-4 py-3 text-2xl font-black text-slate-950 outline-none transition focus:border-hub-400 focus:ring-4 focus:ring-hub-200/60 dark:border-white/15 dark:bg-slate-950/60 dark:text-white dark:focus:ring-hub-400/15"
						bind:value={renameValue}
						disabled={renameSubmitting}
					/>
				</label>
				<Button type="submit" size="sm" disabled={renameSubmitting}>{renameSubmitting ? 'Saving...' : 'Save'}</Button>
				<Button type="button" intent="secondary" size="sm" onclick={() => (renaming = false)} disabled={renameSubmitting}>Cancel</Button>
			</form>
			{#if renameError}
				<p class="mt-3 rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm font-semibold text-red-800 dark:border-red-400/20 dark:bg-red-500/10 dark:text-red-200" role="alert">{renameError}</p>
			{/if}
		{:else}
			<div class="mt-6 flex flex-wrap items-center gap-4">
				<h1 class="text-4xl font-black tracking-tight text-slate-950 dark:text-white sm:text-5xl">{name}</h1>
				{#if isOwner}
					<Button type="button" intent="secondary" size="sm" onclick={startRename}>Rename</Button>
				{/if}
			</div>
		{/if}

		<p class="mt-4 text-sm text-slate-600 dark:text-slate-300">Created {formatDate(data.workspace.created_at)}</p>
	</div>

	<div class="rounded-[2rem] border border-slate-900/10 bg-white/80 p-7 shadow-xl shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20 sm:p-9">
		<h2 class="text-2xl font-bold text-slate-950 dark:text-white">Members</h2>

		<div class="mt-6 overflow-x-auto">
			<table class="w-full min-w-[36rem] text-left text-sm">
				<thead>
					<tr class="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500 dark:text-slate-400">
						<th class="pb-3 pr-4">User</th>
						<th class="pb-3 pr-4">User ID</th>
						<th class="pb-3 pr-4">Role</th>
						<th class="pb-3 pr-4">Joined</th>
						{#if isOwner}
							<th class="pb-3">Actions</th>
						{/if}
					</tr>
				</thead>
				<tbody>
					{#each members as member (member.user_id)}
						<tr class="border-t border-slate-900/10 dark:border-white/10">
							<td class="py-3 pr-4">
								<p class="font-bold text-slate-950 dark:text-white">{member.username}</p>
								<p class="text-slate-500 dark:text-slate-400">{member.email}</p>
							</td>
							<td class="py-3 pr-4 font-mono text-xs text-slate-600 dark:text-slate-300">{member.user_id}</td>
							<td class="py-3 pr-4">
								{#if isOwner}
									<select
										class="rounded-xl border border-slate-300/80 bg-white/80 px-3 py-2 text-sm capitalize text-slate-950 outline-none focus:border-hub-400 dark:border-white/15 dark:bg-slate-950/60 dark:text-white"
										value={member.role}
										disabled={rowBusy === member.user_id}
										onchange={(event) => void changeRole(member, event.currentTarget.value as WorkspaceRole)}
									>
										<option value="owner">Owner</option>
										<option value="editor">Editor</option>
										<option value="viewer">Viewer</option>
									</select>
								{:else}
									<span class="capitalize">{member.role}</span>
								{/if}
							</td>
							<td class="py-3 pr-4 text-slate-600 dark:text-slate-300">{formatDate(member.created_at)}</td>
							{#if isOwner}
								<td class="py-3">
									<Button
										type="button"
										intent="secondary"
										size="sm"
										onclick={() => void removeMember(member)}
										disabled={rowBusy === member.user_id}
									>
										Remove
									</Button>
								</td>
							{/if}
						</tr>
						{#if rowError?.userId === member.user_id}
							<tr>
								<td colspan={isOwner ? 5 : 4} class="pb-3">
									<p class="rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm font-semibold text-red-800 dark:border-red-400/20 dark:bg-red-500/10 dark:text-red-200" role="alert">{rowError.message}</p>
								</td>
							</tr>
						{/if}
					{/each}
				</tbody>
			</table>
		</div>

		{#if isOwner}
			<form class="mt-8 grid gap-4 border-t border-slate-900/10 pt-6 dark:border-white/10 sm:grid-cols-[1fr_10rem_auto] sm:items-end" onsubmit={(event) => { event.preventDefault(); void addMember(); }}>
				<label class="grid gap-2 text-sm font-semibold text-slate-700 dark:text-slate-200">
					User ID
					<input
						class="rounded-2xl border border-slate-300/80 bg-white/80 px-4 py-3 font-mono text-sm text-slate-950 outline-none transition focus:border-hub-400 focus:ring-4 focus:ring-hub-200/60 dark:border-white/15 dark:bg-slate-950/60 dark:text-white dark:focus:ring-hub-400/15"
						bind:value={newMemberId}
						disabled={addingMember}
						placeholder="Their User ID"
					/>
				</label>
				<label class="grid gap-2 text-sm font-semibold text-slate-700 dark:text-slate-200">
					Role
					<select
						class="rounded-2xl border border-slate-300/80 bg-white/80 px-4 py-3 text-sm capitalize text-slate-950 outline-none focus:border-hub-400 dark:border-white/15 dark:bg-slate-950/60 dark:text-white"
						bind:value={newMemberRole}
						disabled={addingMember}
					>
						<option value="owner">Owner</option>
						<option value="editor">Editor</option>
						<option value="viewer">Viewer</option>
					</select>
				</label>
				<Button type="submit" disabled={addingMember}>{addingMember ? 'Adding...' : 'Add member'}</Button>
			</form>
			{#if addMemberError}
				<p class="mt-3 rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm font-semibold text-red-800 dark:border-red-400/20 dark:bg-red-500/10 dark:text-red-200" role="alert">{addMemberError}</p>
			{/if}
		{/if}
	</div>

	{#if isOwner}
		<div class="rounded-[2rem] border border-red-200 bg-red-50/60 p-7 dark:border-red-400/20 dark:bg-red-500/5">
			<h2 class="text-xl font-bold text-red-950 dark:text-red-100">Archive this workspace</h2>
			<p class="mt-2 max-w-2xl text-sm leading-6 text-red-900/80 dark:text-red-100/80">
				Archiving removes this workspace from normal use and stops its deployments. It can be restored later from
				Archived workspaces, until its retention period ends and it is permanently purged.
			</p>

			{#if confirmingArchive}
				<div class="mt-4 flex flex-wrap items-center gap-3">
					<Button type="button" intent="primary" size="sm" onclick={() => void archiveWorkspace()} disabled={archiving}>
						{archiving ? 'Archiving...' : 'Yes, archive this workspace'}
					</Button>
					<Button type="button" intent="secondary" size="sm" onclick={() => (confirmingArchive = false)} disabled={archiving}>
						Cancel
					</Button>
				</div>
			{:else}
				<Button type="button" intent="secondary" size="sm" class="mt-4" onclick={() => (confirmingArchive = true)}>
					Archive workspace
				</Button>
			{/if}

			{#if archiveError}
				<p class="mt-4 rounded-2xl border border-red-300 bg-red-100 px-4 py-3 text-sm font-semibold text-red-900 dark:border-red-400/30 dark:bg-red-500/10 dark:text-red-100" role="alert">{archiveError}</p>
			{/if}
		</div>
	{/if}
</section>
