<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { ApiError, api, normalizeTagInput, type NotebookCreateRequest } from '$lib/api';
	import { getActiveWorkspaceId } from '$lib/stores/activeWorkspace';
	import { auth } from '$lib/stores/auth';
	import { FilePlus, FileUp, Link2, LockKeyhole, LogIn } from '@lucide/svelte';
	import Button from '$lib/components/Button.svelte';
	import { card, errorBanner, fieldError, fieldHint, fieldLabel, input, textarea } from '$lib/design/classes';
	import Callout from '$lib/design/components/Callout.svelte';
	import PageHeader from '$lib/design/components/PageHeader.svelte';
	import WorkspaceTargetPicker from '$lib/components/WorkspaceTargetPicker.svelte';

	type Tab = 'blank' | 'upload' | 'gitlab';
	type FieldErrors = Partial<Record<'title' | 'file' | 'url' | 'server', string>>;

	const blankSource = 'import marimo\n\napp = marimo.App()\n\n\n@app.cell\ndef _():\n    import marimo as mo\n    # Update the Notebook title from its MarimoHub settings.\n    return (mo,)\n\n\nif __name__ == "__main__":\n    app.run()\n';

	// One target survives every tab switch below because this state lives
	// above all three forms and the picker is rendered once, not per-tab.
	// A `?workspace=` param seeds the initial choice (e.g. arriving from a
	// workspace's own page); the picker itself still validates it against the
	// caller's actual writable list before treating it as a real selection.
	let workspaceId = $state(page.url.searchParams.get('workspace') ?? getActiveWorkspaceId() ?? '');

	let activeTab = $state<Tab>('blank');
	let title = $state('Untitled notebook');
	let description = $state('');
	let tags = $state('');
	let uploadTitle = $state('');
	let uploadDescription = $state('');
	let uploadTags = $state('');
	let selectedFile = $state<File | null>(null);
	let gitlabUrl = $state('');
	let gitlabPat = $state('');
	let errors = $state<FieldErrors>({});
	let submitting = $state(false);

	const isAuthenticated = $derived(Boolean($auth.token));

	const tabs: { id: Tab; label: string; icon: typeof FilePlus }[] = [
		{ id: 'blank', label: 'Blank', icon: FilePlus },
		{ id: 'upload', label: 'Upload .py', icon: FileUp },
		{ id: 'gitlab', label: 'GitLab URL', icon: Link2 }
	];

	function metadata(baseTitle: string, baseDescription: string, baseTags: string): Omit<NotebookCreateRequest, 'workspace_id'> {
		return {
			title: baseTitle.trim(),
			description: baseDescription.trim() || null,
			tags: normalizeTagInput(baseTags)
		};
	}

	function authError() {
		errors = { server: 'Sign in before creating notebooks.' };
	}

	function serverError(error: unknown, fallback: string) {
		errors = { server: error instanceof ApiError ? error.detail : fallback };
	}

	async function createNotebook(body: Omit<NotebookCreateRequest, 'workspace_id'>) {
		if (!isAuthenticated) {
			authError();
			return;
		}
		if (!workspaceId) {
			errors = { server: 'Choose a workspace to create this notebook in.' };
			return;
		}

		submitting = true;
		errors = {};
		try {
			const notebook = await api.notebooks.create({ ...body, workspace_id: workspaceId });
			await goto(`/notebooks/${notebook.id}/edit`);
		} catch (error) {
			serverError(error, 'Unable to create notebook. Please try again.');
		} finally {
			submitting = false;
		}
	}

	async function submitBlank() {
		const body = metadata(title, description, tags);
		if (!body.title) {
			errors = { title: 'Name your notebook.' };
			return;
		}

		await createNotebook({ ...body, source: blankSource });
	}

	async function submitUpload() {
		if (!selectedFile) {
			errors = { file: 'Choose a Python file.' };
			return;
		}
		if (!selectedFile.name.endsWith('.py')) {
			errors = { file: 'Upload a .py file.' };
			return;
		}

		submitting = true;
		errors = {};
		try {
			const source = await selectedFile.text();
			const fallbackTitle = selectedFile.name.replace(/\.py$/i, '').replace(/[-_]+/g, ' ');
			const body = metadata(uploadTitle || fallbackTitle, uploadDescription, uploadTags);
			if (!body.title) {
				errors = { title: 'Name your notebook.' };
				return;
			}
			await createNotebook({ ...body, source });
		} catch (error) {
			serverError(error, 'Unable to read or upload that file. Please try again.');
		} finally {
			submitting = false;
		}
	}

	async function submitGitLab() {
		if (!isAuthenticated) {
			authError();
			return;
		}
		if (!workspaceId) {
			errors = { server: 'Choose a workspace to import this notebook into.' };
			return;
		}

		const url = gitlabUrl.trim();
		if (!url) {
			errors = { url: 'Enter a GitLab raw file URL.' };
			return;
		}

		submitting = true;
		errors = {};
		try {
			const notebook = await api.notebooks.import({ url, pat: gitlabPat.trim() || undefined, workspace_id: workspaceId });
			await goto(`/notebooks/${notebook.id}/edit`);
		} catch (error) {
			serverError(error, 'Unable to import that GitLab notebook. Please check the URL and try again.');
		} finally {
			submitting = false;
			// Cleared once this request is settled either way: the token is only
			// ever needed for the one fetch inside `import_gitlab_notebook`, and
			// must never persist in this page's state afterward.
			gitlabPat = '';
		}
	}

	function selectFile(event: Event) {
		const input = event.currentTarget as HTMLInputElement;
		selectedFile = input.files?.[0] ?? null;
		if (selectedFile && !uploadTitle) uploadTitle = selectedFile.name.replace(/\.py$/i, '').replace(/[-_]+/g, ' ');
		errors = {};
	}

	function switchTab(tab: Tab) {
		activeTab = tab;
		errors = {};
	}
</script>

<svelte:head>
	<title>Create notebook | MarimoHub</title>
</svelte:head>

<PageHeader
	title="Start a marimo notebook."
	eyebrow="Create"
	icon={FilePlus}
	description="Every new notebook starts as a Private Notebook in the Workspace you choose below, built from a blank notebook, a local Python file, or a GitLab raw file URL."
/>

{#if !isAuthenticated}
	<Callout class="mb-5 max-w-3xl">
		<p class="font-semibold">Authentication required</p>
		<p class="mt-1 text-app-muted">Notebook creation and imports require an account.</p>
		<div class="mt-3 flex flex-wrap gap-2">
			<Button intent="primary" size="sm" href="/auth/login"><LogIn size={14} />Sign in</Button>
			<Button intent="secondary" size="sm" href="/auth/register">Create account</Button>
		</div>
	</Callout>
{/if}

<div class="{card} max-w-4xl">
	{#if isAuthenticated}
		<div class="border-b border-app-line p-5">
			<WorkspaceTargetPicker bind:value={workspaceId} id="workspace" />
		</div>
	{/if}

	<div class="flex gap-1 overflow-x-auto border-b border-app-line px-3" role="tablist" aria-label="Notebook creation method">
		{#each tabs as tab (tab.id)}
			{@const Icon = tab.icon}
			<button
				class={`relative inline-flex shrink-0 items-center gap-2 px-3 py-3 text-sm font-medium transition ${activeTab === tab.id ? 'text-brand-strong' : 'text-app-muted hover:text-app-fg'}`}
				type="button"
				role="tab"
				aria-selected={activeTab === tab.id}
				onclick={() => switchTab(tab.id)}
			>
				<Icon size={15} />{tab.label}
				{#if activeTab === tab.id}<span class="absolute inset-x-2 bottom-[-1px] h-0.5 bg-brand"></span>{/if}
			</button>
		{/each}
	</div>

	<div class="p-5">
		{#if errors.server}
			<p class="{errorBanner} mb-5" role="alert">{errors.server}</p>
		{/if}

		{#if activeTab === 'blank'}
			<form class="grid gap-4" onsubmit={(event) => { event.preventDefault(); void submitBlank(); }} novalidate>
				<div class="grid gap-4 md:grid-cols-2">
					<div class="grid gap-1.5 content-start">
						<label class={fieldLabel} for="title">Title</label>
						<input class={input} id="title" bind:value={title} aria-invalid={Boolean(errors.title)} />
						{#if errors.title}<p class={fieldError}>{errors.title}</p>{/if}
					</div>
					<div class="grid gap-1.5 content-start">
						<label class={fieldLabel} for="tags">Tags</label>
						<input class={input} id="tags" bind:value={tags} placeholder="signals, demo" />
					</div>
				</div>
				<div class="grid gap-1.5">
					<label class={fieldLabel} for="description">Description</label>
					<textarea class={textarea} id="description" bind:value={description}></textarea>
				</div>
				<div class="flex justify-end border-t border-app-line pt-4">
					<Button intent="primary" type="submit" disabled={submitting || !isAuthenticated || !workspaceId}><FilePlus size={15} />{submitting ? 'Creating...' : 'Create blank notebook'}</Button>
				</div>
			</form>
		{:else if activeTab === 'upload'}
			<form class="grid gap-4" onsubmit={(event) => { event.preventDefault(); void submitUpload(); }} novalidate>
				<div class="rounded-lg border border-dashed border-app-line-strong bg-app-bg p-5">
					<label class={fieldLabel} for="file">Python source file</label>
					<input
						class="mt-2 block w-full text-sm text-app-muted file:mr-4 file:h-9 file:cursor-pointer file:rounded-md file:border-0 file:bg-brand file:px-3.5 file:text-sm file:font-semibold file:text-on-brand"
						id="file"
						type="file"
						accept=".py,text/x-python"
						onchange={selectFile}
					/>
					<p class="{fieldHint} mt-2">The file is read in your browser and sent as notebook source.</p>
					{#if errors.file}<p class="{fieldError} mt-1">{errors.file}</p>{/if}
				</div>
				<div class="grid gap-4 md:grid-cols-2">
					<div class="grid gap-1.5 content-start">
						<label class={fieldLabel} for="upload-title">Title</label>
						<input class={input} id="upload-title" bind:value={uploadTitle} placeholder="Defaults to filename" aria-invalid={Boolean(errors.title)} />
						{#if errors.title}<p class={fieldError}>{errors.title}</p>{/if}
					</div>
					<div class="grid gap-1.5 content-start">
						<label class={fieldLabel} for="upload-tags">Tags</label>
						<input class={input} id="upload-tags" bind:value={uploadTags} placeholder="analysis, teaching" />
					</div>
				</div>
				<div class="grid gap-1.5">
					<label class={fieldLabel} for="upload-description">Description</label>
					<textarea class={textarea} id="upload-description" bind:value={uploadDescription}></textarea>
				</div>
				<div class="flex justify-end border-t border-app-line pt-4">
					<Button intent="primary" type="submit" disabled={submitting || !isAuthenticated || !workspaceId}><FileUp size={15} />{submitting ? 'Uploading...' : 'Create from file'}</Button>
				</div>
			</form>
		{:else}
			<form class="grid gap-4" onsubmit={(event) => { event.preventDefault(); void submitGitLab(); }} novalidate>
				<div class="grid gap-1.5">
					<label class={fieldLabel} for="gitlab-url">GitLab raw file URL</label>
					<input class="{input} font-mono" id="gitlab-url" type="url" bind:value={gitlabUrl} placeholder="https://gitlab.com/.../-/raw/main/notebook.py" aria-invalid={Boolean(errors.url)} />
					{#if errors.url}<p class={fieldError}>{errors.url}</p>{/if}
				</div>
				<div class="grid gap-1.5">
					<label class={fieldLabel} for="gitlab-pat">Personal access token <span class="font-normal">optional</span></label>
					<span class="relative block">
						<LockKeyhole size={15} class="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-app-muted" />
						<input class="{input} pl-9" id="gitlab-pat" type="password" autocomplete="off" bind:value={gitlabPat} placeholder="Only needed for private files" />
					</span>
					<p class={fieldHint}>The token is sent once to fetch this file and is never stored by MarimoHub.</p>
				</div>
				<div class="flex justify-end border-t border-app-line pt-4">
					<Button intent="primary" type="submit" disabled={submitting || !isAuthenticated || !workspaceId}><Link2 size={15} />{submitting ? 'Importing...' : 'Import from GitLab'}</Button>
				</div>
			</form>
		{/if}
	</div>
</div>
