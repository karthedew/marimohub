<script lang="ts">
	import { workspaces as workspaceStore } from '$lib/stores/workspaces';
	import Button from './Button.svelte';
	import { nextTargetValue, targetOptionLabel, targetPickerState } from './workspaceTargetPicker';

	type Props = {
		value: string;
		id?: string;
		label?: string;
	};

	let { value = $bindable(''), id = 'workspace-target', label = 'Workspace' }: Props = $props();

	const state = $derived(targetPickerState($workspaceStore));

	// The only place this component ever writes `value` on its own: forcing the
	// sole writable target, clearing a selection that fell out of the list, and
	// otherwise leaving a deliberate multi-target choice alone across re-renders.
	$effect(() => {
		const next = nextTargetValue(state, value);
		if (next !== value) value = next;
	});
</script>

<div>
	<label class="text-sm font-bold text-slate-800 dark:text-slate-100" for={id}>{label}</label>

	{#if state.kind === 'loading'}
		<p class="mt-2 text-sm font-semibold text-slate-600 dark:text-slate-300">Loading your workspaces...</p>
	{:else if state.kind === 'error'}
		<div class="mt-2">
			<p class="text-sm font-semibold text-red-700 dark:text-red-300" role="alert">{state.message}</p>
			<Button intent="secondary" size="sm" class="mt-3" type="button" onclick={() => void workspaceStore.refresh()}>Retry</Button>
		</div>
	{:else if state.kind === 'empty'}
		<div class="mt-2">
			<p class="text-sm font-semibold text-slate-600 dark:text-slate-300">You need write access to a Workspace first.</p>
			<Button intent="secondary" size="sm" class="mt-3" href="/workspaces">Go to Workspaces</Button>
		</div>
	{:else if state.kind === 'single'}
		<select
			class="mt-2 w-full max-w-sm rounded-2xl border border-slate-300 bg-slate-100 px-4 py-3 text-slate-950 outline-none dark:border-white/15 dark:bg-slate-950/60 dark:text-white"
			{id}
			value={state.workspace.id}
			disabled
		>
			<option value={state.workspace.id}>{targetOptionLabel(state.workspace, [state.workspace])}</option>
		</select>
	{:else}
		<select
			class="mt-2 w-full max-w-sm rounded-2xl border border-slate-300 bg-white px-4 py-3 text-slate-950 outline-none transition focus:border-hub-500 focus:ring-4 focus:ring-hub-500/15 dark:border-white/15 dark:bg-slate-950/50 dark:text-white"
			{id}
			bind:value
		>
			<option value="" disabled>Choose a Workspace</option>
			{#each state.workspaces as workspace (workspace.id)}
				<option value={workspace.id}>{targetOptionLabel(workspace, state.workspaces)}</option>
			{/each}
		</select>
	{/if}
</div>
