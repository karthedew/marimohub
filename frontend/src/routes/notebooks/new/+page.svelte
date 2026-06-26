<script lang="ts">
	import { goto } from '$app/navigation';
	import { ApiError, api, normalizeTagInput, type NotebookCreateRequest } from '$lib/api';
	import { auth } from '$lib/stores/auth';

	type Tab = 'blank' | 'upload' | 'gitlab';
	type FieldErrors = Partial<Record<'title' | 'file' | 'url' | 'server', string>>;

	const blankSource = 'import marimo as mo\n\napp = mo.App()\n\n\n@app.cell\ndef _():\n    mo.md("# Untitled notebook")\n    return\n\n\nif __name__ == "__main__":\n    app.run()\n';

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

	function metadata(baseTitle: string, baseDescription: string, baseTags: string): NotebookCreateRequest {
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

	async function createNotebook(body: NotebookCreateRequest) {
		if (!isAuthenticated) {
			authError();
			return;
		}

		submitting = true;
		errors = {};
		try {
			const notebook = await api.notebooks.create(body);
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

		const url = gitlabUrl.trim();
		if (!url) {
			errors = { url: 'Enter a GitLab raw file URL.' };
			return;
		}

		submitting = true;
		errors = {};
		try {
			const notebook = await api.notebooks.import({ url, pat: gitlabPat.trim() || undefined });
			gitlabPat = '';
			await goto(`/notebooks/${notebook.id}/edit`);
		} catch (error) {
			serverError(error, 'Unable to import that GitLab notebook. Please check the URL and try again.');
		} finally {
			submitting = false;
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
	<title>Create notebook | MoLab</title>
</svelte:head>

<section class="space-y-8">
	<div class="grid gap-6 lg:grid-cols-[minmax(0,1fr)_22rem] lg:items-end">
		<div class="space-y-4">
			<p class="w-fit rounded-full bg-orange-100 px-4 py-2 text-sm font-semibold text-orange-900 dark:bg-orange-400/10 dark:text-orange-200">
				Create
			</p>
			<h1 class="text-4xl font-black tracking-tight text-slate-950 dark:text-white sm:text-6xl">Start a marimo notebook.</h1>
			<p class="max-w-2xl text-lg leading-8 text-slate-700 dark:text-slate-300">
				Create a private draft from a blank notebook, a local Python file, or a GitLab raw file URL.
			</p>
		</div>

		{#if !isAuthenticated}
			<div class="rounded-[2rem] border border-orange-200 bg-orange-50 p-5 text-sm leading-6 text-orange-950 shadow-lg shadow-orange-950/5 dark:border-orange-300/20 dark:bg-orange-400/10 dark:text-orange-100">
				<p class="font-black">Authentication required</p>
				<p class="mt-2">Notebook creation and imports require an account.</p>
				<div class="mt-4 flex flex-wrap gap-2">
					<a class="rounded-full bg-graphite px-4 py-2 font-bold text-white dark:bg-white dark:text-slate-950" href="/auth/login">Sign in</a>
					<a class="rounded-full border border-orange-300 px-4 py-2 font-bold dark:border-orange-200/30" href="/auth/register">Create account</a>
				</div>
			</div>
		{/if}
	</div>

	<div class="rounded-[2rem] border border-slate-900/10 bg-white/80 p-4 shadow-xl shadow-slate-900/5 backdrop-blur dark:border-white/10 dark:bg-white/10 dark:shadow-black/20 sm:p-6">
		<div class="grid gap-2 rounded-[1.5rem] bg-slate-100 p-2 dark:bg-slate-950/40 sm:grid-cols-3" role="tablist" aria-label="Notebook creation method">
			<button class="rounded-full px-4 py-3 text-sm font-black transition {activeTab === 'blank' ? 'bg-white text-slate-950 shadow-sm dark:bg-white dark:text-slate-950' : 'text-slate-600 hover:bg-white/60 dark:text-slate-300 dark:hover:bg-white/10'}" type="button" role="tab" aria-selected={activeTab === 'blank'} onclick={() => switchTab('blank')}>Blank</button>
			<button class="rounded-full px-4 py-3 text-sm font-black transition {activeTab === 'upload' ? 'bg-white text-slate-950 shadow-sm dark:bg-white dark:text-slate-950' : 'text-slate-600 hover:bg-white/60 dark:text-slate-300 dark:hover:bg-white/10'}" type="button" role="tab" aria-selected={activeTab === 'upload'} onclick={() => switchTab('upload')}>Upload .py</button>
			<button class="rounded-full px-4 py-3 text-sm font-black transition {activeTab === 'gitlab' ? 'bg-white text-slate-950 shadow-sm dark:bg-white dark:text-slate-950' : 'text-slate-600 hover:bg-white/60 dark:text-slate-300 dark:hover:bg-white/10'}" type="button" role="tab" aria-selected={activeTab === 'gitlab'} onclick={() => switchTab('gitlab')}>GitLab URL</button>
		</div>

		{#if errors.server}
			<p class="mt-5 rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm font-semibold text-red-800 dark:border-red-400/20 dark:bg-red-500/10 dark:text-red-200">{errors.server}</p>
		{/if}

		{#if activeTab === 'blank'}
			<form class="mt-6 grid gap-5" onsubmit={(event) => { event.preventDefault(); void submitBlank(); }} novalidate>
				<div class="grid gap-5 md:grid-cols-2">
					<div>
						<label class="text-sm font-bold text-slate-800 dark:text-slate-100" for="title">Title</label>
						<input class="mt-2 w-full rounded-2xl border border-slate-300 bg-white px-4 py-3 text-slate-950 outline-none transition focus:border-orange-500 focus:ring-4 focus:ring-orange-500/15 dark:border-white/15 dark:bg-slate-950/50 dark:text-white" id="title" bind:value={title} aria-invalid={Boolean(errors.title)} />
						{#if errors.title}<p class="mt-2 text-sm font-semibold text-red-700 dark:text-red-300">{errors.title}</p>{/if}
					</div>
					<div>
						<label class="text-sm font-bold text-slate-800 dark:text-slate-100" for="tags">Tags</label>
						<input class="mt-2 w-full rounded-2xl border border-slate-300 bg-white px-4 py-3 text-slate-950 outline-none transition focus:border-orange-500 focus:ring-4 focus:ring-orange-500/15 dark:border-white/15 dark:bg-slate-950/50 dark:text-white" id="tags" bind:value={tags} placeholder="signals, demo" />
					</div>
				</div>
				<div>
					<label class="text-sm font-bold text-slate-800 dark:text-slate-100" for="description">Description</label>
					<textarea class="mt-2 min-h-28 w-full rounded-2xl border border-slate-300 bg-white px-4 py-3 text-slate-950 outline-none transition focus:border-orange-500 focus:ring-4 focus:ring-orange-500/15 dark:border-white/15 dark:bg-slate-950/50 dark:text-white" id="description" bind:value={description}></textarea>
				</div>
				<button class="w-fit rounded-full bg-graphite px-5 py-3 text-sm font-bold text-white shadow-lg shadow-slate-950/10 disabled:cursor-not-allowed disabled:opacity-60 dark:bg-white dark:text-slate-950" type="submit" disabled={submitting || !isAuthenticated}>{submitting ? 'Creating...' : 'Create blank notebook'}</button>
			</form>
		{:else if activeTab === 'upload'}
			<form class="mt-6 grid gap-5" onsubmit={(event) => { event.preventDefault(); void submitUpload(); }} novalidate>
				<div class="rounded-[1.5rem] border border-dashed border-slate-300 bg-slate-50 p-5 dark:border-white/15 dark:bg-slate-950/30">
					<label class="text-sm font-bold text-slate-800 dark:text-slate-100" for="file">Python source file</label>
					<input class="mt-3 block w-full text-sm font-semibold text-slate-700 file:mr-4 file:rounded-full file:border-0 file:bg-graphite file:px-4 file:py-2 file:text-sm file:font-bold file:text-white dark:text-slate-200 dark:file:bg-white dark:file:text-slate-950" id="file" type="file" accept=".py,text/x-python" onchange={selectFile} />
					<p class="mt-3 text-sm text-slate-600 dark:text-slate-300">The file is read in your browser and sent as notebook source.</p>
					{#if errors.file}<p class="mt-2 text-sm font-semibold text-red-700 dark:text-red-300">{errors.file}</p>{/if}
				</div>
				<div class="grid gap-5 md:grid-cols-2">
					<div>
						<label class="text-sm font-bold text-slate-800 dark:text-slate-100" for="upload-title">Title</label>
						<input class="mt-2 w-full rounded-2xl border border-slate-300 bg-white px-4 py-3 text-slate-950 outline-none transition focus:border-orange-500 focus:ring-4 focus:ring-orange-500/15 dark:border-white/15 dark:bg-slate-950/50 dark:text-white" id="upload-title" bind:value={uploadTitle} placeholder="Defaults to filename" aria-invalid={Boolean(errors.title)} />
						{#if errors.title}<p class="mt-2 text-sm font-semibold text-red-700 dark:text-red-300">{errors.title}</p>{/if}
					</div>
					<div>
						<label class="text-sm font-bold text-slate-800 dark:text-slate-100" for="upload-tags">Tags</label>
						<input class="mt-2 w-full rounded-2xl border border-slate-300 bg-white px-4 py-3 text-slate-950 outline-none transition focus:border-orange-500 focus:ring-4 focus:ring-orange-500/15 dark:border-white/15 dark:bg-slate-950/50 dark:text-white" id="upload-tags" bind:value={uploadTags} placeholder="analysis, teaching" />
					</div>
				</div>
				<div>
					<label class="text-sm font-bold text-slate-800 dark:text-slate-100" for="upload-description">Description</label>
					<textarea class="mt-2 min-h-28 w-full rounded-2xl border border-slate-300 bg-white px-4 py-3 text-slate-950 outline-none transition focus:border-orange-500 focus:ring-4 focus:ring-orange-500/15 dark:border-white/15 dark:bg-slate-950/50 dark:text-white" id="upload-description" bind:value={uploadDescription}></textarea>
				</div>
				<button class="w-fit rounded-full bg-graphite px-5 py-3 text-sm font-bold text-white shadow-lg shadow-slate-950/10 disabled:cursor-not-allowed disabled:opacity-60 dark:bg-white dark:text-slate-950" type="submit" disabled={submitting || !isAuthenticated}>{submitting ? 'Uploading...' : 'Create from file'}</button>
			</form>
		{:else}
			<form class="mt-6 grid gap-5" onsubmit={(event) => { event.preventDefault(); void submitGitLab(); }} novalidate>
				<div>
					<label class="text-sm font-bold text-slate-800 dark:text-slate-100" for="gitlab-url">GitLab raw file URL</label>
					<input class="mt-2 w-full rounded-2xl border border-slate-300 bg-white px-4 py-3 text-slate-950 outline-none transition focus:border-orange-500 focus:ring-4 focus:ring-orange-500/15 dark:border-white/15 dark:bg-slate-950/50 dark:text-white" id="gitlab-url" type="url" bind:value={gitlabUrl} placeholder="https://gitlab.com/.../-/raw/main/notebook.py" aria-invalid={Boolean(errors.url)} />
					{#if errors.url}<p class="mt-2 text-sm font-semibold text-red-700 dark:text-red-300">{errors.url}</p>{/if}
				</div>
				<div>
					<label class="text-sm font-bold text-slate-800 dark:text-slate-100" for="gitlab-pat">Personal access token <span class="font-semibold text-slate-500 dark:text-slate-400">optional</span></label>
					<input class="mt-2 w-full rounded-2xl border border-slate-300 bg-white px-4 py-3 text-slate-950 outline-none transition focus:border-orange-500 focus:ring-4 focus:ring-orange-500/15 dark:border-white/15 dark:bg-slate-950/50 dark:text-white" id="gitlab-pat" type="password" autocomplete="off" bind:value={gitlabPat} placeholder="Only needed for private files" />
					<p class="mt-2 text-sm font-semibold text-slate-600 dark:text-slate-300">The token is sent once to fetch this file and is never stored by MoLab.</p>
				</div>
				<button class="w-fit rounded-full bg-graphite px-5 py-3 text-sm font-bold text-white shadow-lg shadow-slate-950/10 disabled:cursor-not-allowed disabled:opacity-60 dark:bg-white dark:text-slate-950" type="submit" disabled={submitting || !isAuthenticated}>{submitting ? 'Importing...' : 'Import from GitLab'}</button>
			</form>
		{/if}
	</div>
</section>
