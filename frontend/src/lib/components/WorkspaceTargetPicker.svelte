<script lang="ts">
	import { RotateCcw } from '@lucide/svelte';
	import { fieldLabel, select } from '$lib/design/classes';
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

<div class="grid gap-1.5">
	<label class={fieldLabel} for={id}>{label}</label>

	{#if state.kind === 'loading'}
		<p class="m-0 text-sm text-app-muted">Loading your workspaces...</p>
	{:else if state.kind === 'error'}
		<div>
			<p class="m-0 text-sm font-medium text-app-danger" role="alert">{state.message}</p>
			<Button intent="secondary" size="sm" class="mt-2" type="button" onclick={() => void workspaceStore.refresh()}>
				<RotateCcw size={14} /> Retry
			</Button>
		</div>
	{:else if state.kind === 'empty'}
		<div>
			<p class="m-0 text-sm text-app-muted">You need write access to a Workspace first.</p>
			<Button intent="secondary" size="sm" class="mt-2" href="/workspaces">Go to Workspaces</Button>
		</div>
	{:else if state.kind === 'single'}
		<select class="{select} max-w-sm" {id} value={state.workspace.id} disabled>
			<option value={state.workspace.id}>{targetOptionLabel(state.workspace, [state.workspace])}</option>
		</select>
	{:else}
		<select class="{select} max-w-sm" {id} bind:value>
			<option value="" disabled>Choose a Workspace</option>
			{#each state.workspaces as workspace (workspace.id)}
				<option value={workspace.id}>{targetOptionLabel(workspace, state.workspaces)}</option>
			{/each}
		</select>
	{/if}
</div>
