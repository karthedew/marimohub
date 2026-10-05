<script lang="ts">
	import { onDestroy, tick } from 'svelte';
	import { afterNavigate, goto, invalidateAll } from '$app/navigation';
	import { ApiError, api, type WorkspaceMember, type WorkspaceRole } from '$lib/api';
	import { displayNameOf, personInitials } from '$lib/people';
	import { workspaces } from '$lib/stores/workspaces';
	import { Archive, FolderKanban, Pencil, Plus, UserPlus, Users } from '@lucide/svelte';
	import Button from '$lib/components/Button.svelte';
	import PersonSearch from '$lib/components/PersonSearch.svelte';
	import { createPersonSearch } from '$lib/components/personSearch';
	import { card, cardHeader, cardTitle, errorBanner, fieldHint, fieldLabel, input, select } from '$lib/design/classes';
	import Badge from '$lib/design/components/Badge.svelte';
	import Breadcrumbs from '$lib/design/components/Breadcrumbs.svelte';
	import DataTable from '$lib/design/components/DataTable.svelte';

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

	// Owners find the person to add by name, username, exact email address, or
	// a pasted User ID; adding still sends only the chosen person's `user_id`.
	// The workspace id is read when each search runs, not captured once:
	// SvelteKit keeps this page mounted when navigating between workspaces.
	const personSearch = createPersonSearch({
		search: (query, signal) => api.workspaces.members.candidates(data.workspace.id, query, { signal })
	});
	const personToAdd = $derived($personSearch.selected);
	let newMemberRole = $state<WorkspaceRole>('editor');
	let addMemberError = $state<string | null>(null);
	let addingMember = $state(false);

	onDestroy(() => personSearch.destroy());

	// Another workspace's page starts its add-member form from scratch.
	afterNavigate(() => resetAddMember());

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

	function resetAddMember() {
		personSearch.reset();
		newMemberRole = 'editor';
		addMemberError = null;
	}

	async function addMember() {
		addMemberError = null;
		const person = personToAdd;
		if (!person) {
			addMemberError = 'Choose a person to add.';
			return;
		}

		addingMember = true;
		try {
			await api.workspaces.members.add(data.workspace.id, {
				user_id: person.user_id,
				role: newMemberRole
			});
			resetAddMember();
			await Promise.all([invalidateAll(), workspaces.refresh()]);
		} catch (caught) {
			addMemberError = caught instanceof ApiError ? caught.detail : 'Unable to add member.';
		} finally {
			addingMember = false;
		}

		// The form was disabled while adding, so focus has fallen to the page;
		// hand it back to the Person field. After an add it is ready for the next
		// person; after a failure it still holds the person it failed for, so
		// Enter retries and typing picks someone else. Focus the caller moved
		// somewhere else in the meantime stays where it is.
		await tick();
		if (document.activeElement === null || document.activeElement === document.body) {
			document.getElementById('member-person')?.focus();
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

<Breadcrumbs crumbs={[{ label: 'Workspaces', href: '/workspaces' }, { label: name, href: `/workspaces/${data.workspace.id}` }]} />

<div class="mb-7 flex flex-col justify-between gap-4 sm:flex-row sm:items-end">
	<div class="flex min-w-0 items-start gap-3">
		<span class="mt-1 grid size-11 shrink-0 place-items-center rounded-lg bg-brand-soft text-brand-strong"><FolderKanban size={21} /></span>
		<div class="min-w-0">
			<div class="flex flex-wrap items-center gap-2 text-xs text-app-muted">
				<span class="font-mono">{data.workspace.slug}</span>
				<Badge tone={isOwner ? 'accent' : 'neutral'}>{data.workspace.role}</Badge>
			</div>
			{#if renaming}
				<form class="mt-2 flex flex-wrap items-end gap-2" onsubmit={(event) => { event.preventDefault(); void submitRename(); }}>
					<div class="grid gap-1.5">
						<label class={fieldLabel} for="workspace-rename">Name</label>
						<input class="{input} w-72 max-w-full text-base font-semibold" id="workspace-rename" bind:value={renameValue} disabled={renameSubmitting} />
					</div>
					<Button type="submit" disabled={renameSubmitting}>{renameSubmitting ? 'Saving...' : 'Save'}</Button>
					<Button type="button" intent="secondary" onclick={() => (renaming = false)} disabled={renameSubmitting}>Cancel</Button>
				</form>
				{#if renameError}
					<p class="{errorBanner} mt-3" role="alert">{renameError}</p>
				{/if}
			{:else}
				<h1 class="mt-1 break-words">{name}</h1>
			{/if}
			<p class="mt-1.5 text-sm text-app-muted">Created {formatDate(data.workspace.created_at)}</p>
		</div>
	</div>
	<div class="flex shrink-0 flex-wrap items-center gap-2">
		{#if isOwner && !renaming}
			<Button type="button" intent="secondary" onclick={startRename}><Pencil size={14} />Rename</Button>
		{/if}
		{#if canCreateNotebooks}
			<Button intent="primary" href={`/notebooks/new?workspace=${data.workspace.id}`}><Plus size={16} strokeWidth={2.4} />New Notebook</Button>
		{/if}
	</div>
</div>

<!-- No overflow clipping on this card: the Person search popup has to extend past its bottom edge. -->
<section class={card} aria-labelledby="members-heading">
	<div class="{cardHeader} flex items-center gap-3">
		<span class="grid size-9 place-items-center rounded-md bg-brand-soft text-brand-strong"><Users size={18} /></span>
		<div>
			<h2 id="members-heading" class={cardTitle}>Members</h2>
			<p class="mt-0.5 text-xs text-app-muted">{members.length} {members.length === 1 ? 'member' : 'members'} · Owners administer, Editors write, Viewers read</p>
		</div>
	</div>

	<DataTable caption="Workspace members" minWidth="40rem">
		{#snippet head()}
			<tr>
				<th>User</th>
				<th>User ID</th>
				<th>Role</th>
				<th>Joined</th>
				{#if isOwner}<th class="text-right">Actions</th>{/if}
			</tr>
		{/snippet}
		{#each members as member (member.user_id)}
			{@const memberName = displayNameOf(member)}
			<tr>
				<td>
					<div class="flex items-center gap-3">
						<span class="grid size-8 shrink-0 place-items-center rounded-full border border-app-line bg-app-bg text-[11px] font-semibold text-app-muted" aria-hidden="true">{personInitials(member)}</span>
						<!--
							Capped: in an auto-layout table a line that never wraps sizes its
							column, so without a cap `truncate` never cuts a long name, and the
							name pushes Role and Remove out of view. A long display name gives
							way before the username, which is the unique one.
						-->
						<div class="min-w-0 max-w-64">
							<p class="m-0 flex min-w-0 items-baseline gap-1.5 font-semibold text-app-fg" title={memberName ? `${memberName} @${member.username}` : member.username}>
								{#if memberName}
									<span class="truncate">{memberName}</span>
									<span class="max-w-40 shrink-0 truncate font-normal text-app-muted">@{member.username}</span>
								{:else}
									<span class="truncate">{member.username}</span>
								{/if}
							</p>
							<p class="m-0 truncate text-xs text-app-muted" title={member.email}>{member.email}</p>
						</div>
					</div>
				</td>
				<td class="font-mono text-xs text-app-muted">{member.user_id}</td>
				<td>
					{#if isOwner}
						<select
							class="{select} h-9 w-32 capitalize"
							value={member.role}
							disabled={rowBusy === member.user_id}
							onchange={(event) => void changeRole(member, event.currentTarget.value as WorkspaceRole)}
						>
							<option value="owner">Owner</option>
							<option value="editor">Editor</option>
							<option value="viewer">Viewer</option>
						</select>
					{:else}
						<Badge tone={member.role === 'owner' ? 'accent' : 'neutral'}>{member.role}</Badge>
					{/if}
				</td>
				<td class="whitespace-nowrap text-xs text-app-muted">{formatDate(member.created_at)}</td>
				{#if isOwner}
					<td class="text-right">
						<Button type="button" intent="ghost" size="sm" onclick={() => void removeMember(member)} disabled={rowBusy === member.user_id}>
							Remove
						</Button>
					</td>
				{/if}
			</tr>
			{#if rowError?.userId === member.user_id}
				<tr>
					<td colspan={isOwner ? 5 : 4}>
						<p class={errorBanner} role="alert">{rowError.message}</p>
					</td>
				</tr>
			{/if}
		{/each}
	</DataTable>

	{#if isOwner}
		<!--
			One column on phones, in reading order. From `sm` up the field groups
			dissolve (`contents`) into one grid: labels on the first row, controls on
			the second, so Role and the button line up with the Person input while
			its helper text sits directly beneath it.
		-->
		<form
			class="grid gap-4 rounded-b-lg border-t border-app-line bg-app-bg/50 p-5 sm:grid-cols-[minmax(0,1fr)_10rem_auto] sm:gap-x-3 sm:gap-y-1.5"
			onsubmit={(event) => { event.preventDefault(); void addMember(); }}
		>
			<div class="grid gap-1.5 sm:contents">
				<label class="{fieldLabel} sm:col-start-1 sm:row-start-1" for="member-person">Person</label>
				<div class="sm:col-start-1 sm:row-start-2">
					<PersonSearch controller={personSearch} id="member-person" describedby="member-person-help" disabled={addingMember} />
				</div>
				<p class="{fieldHint} sm:col-start-1 sm:row-start-3" id="member-person-help">
					Search by name or username, or by someone's exact email address. A pasted User ID also works.
				</p>
			</div>
			<div class="grid gap-1.5 sm:contents">
				<label class="{fieldLabel} sm:col-start-2 sm:row-start-1" for="member-role">Role</label>
				<select class="{select} bg-app-card capitalize sm:col-start-2 sm:row-start-2" id="member-role" bind:value={newMemberRole} disabled={addingMember}>
					<option value="owner">Owner</option>
					<option value="editor">Editor</option>
					<option value="viewer">Viewer</option>
				</select>
			</div>
			<Button type="submit" class="h-10 sm:col-start-3 sm:row-start-2" disabled={addingMember || !personToAdd}>
				<UserPlus size={15} />{addingMember ? 'Adding...' : 'Add member'}
			</Button>
			{#if addMemberError}
				<p class="{errorBanner} sm:col-span-3 sm:row-start-4 sm:mt-2" role="alert">{addMemberError}</p>
			{/if}
		</form>
	{/if}
</section>

{#if isOwner}
	<section class="mt-5 rounded-lg border border-app-danger/35 bg-app-card p-5" aria-labelledby="archive-heading">
		<div class="flex items-start gap-3">
			<span class="grid size-9 shrink-0 place-items-center rounded-md bg-app-danger/10 text-app-danger"><Archive size={18} /></span>
			<div class="min-w-0 flex-1">
				<h2 id="archive-heading" class={cardTitle}>Archive this workspace</h2>
				<p class="mt-1 max-w-2xl text-sm leading-6 text-app-muted">
					Archiving removes this workspace from normal use and stops its deployments. It can be restored later from
					Archived workspaces, until its retention period ends and it is permanently purged.
				</p>

				{#if confirmingArchive}
					<div class="mt-4 flex flex-wrap items-center gap-2">
						<Button type="button" intent="danger" onclick={() => void archiveWorkspace()} disabled={archiving}>
							{archiving ? 'Archiving...' : 'Yes, archive this workspace'}
						</Button>
						<Button type="button" intent="secondary" onclick={() => (confirmingArchive = false)} disabled={archiving}>
							Cancel
						</Button>
					</div>
				{:else}
					<Button type="button" intent="secondary" class="mt-4" onclick={() => (confirmingArchive = true)}>
						Archive workspace
					</Button>
				{/if}

				{#if archiveError}
					<p class="{errorBanner} mt-4" role="alert">{archiveError}</p>
				{/if}
			</div>
		</div>
	</section>
{/if}
