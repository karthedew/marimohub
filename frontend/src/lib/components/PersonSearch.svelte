<script lang="ts">
	import { CircleAlert, CircleCheck, LoaderCircle, Search } from '@lucide/svelte';
	import { input as inputClass } from '$lib/design/classes';
	import { displayNameOf, personInitials } from '$lib/people';
	import {
		MAX_QUERY_LENGTH,
		candidateOptionLabel,
		isListboxOpen,
		mayHaveMoreMatches,
		noMatchHint,
		resultCountText,
		type PersonSearchController
	} from './personSearch';

	// An editable combobox with list autocomplete (WAI-ARIA APG pattern). All
	// behavior lives in `personSearch.ts`; this only renders its state and
	// forwards events. The page supplies the `<label for={id}>` and the
	// helper text, so the field lays out like any other in its form.
	type Props = {
		controller: PersonSearchController;
		id: string;
		describedby?: string;
		placeholder?: string;
		disabled?: boolean;
	};

	let { controller, id, describedby, placeholder = 'Name, username, or email', disabled = false }: Props = $props();

	const listboxId = $derived(`${id}-listbox`);
	const expanded = $derived(isListboxOpen($controller));
	const activeOptionId = $derived(
		expanded && $controller.activeIndex >= 0 ? optionId($controller.activeIndex) : undefined
	);

	function optionId(index: number) {
		return `${id}-option-${index}`;
	}

	// Keeps the option the keyboard is on in view inside a scrolled listbox.
	$effect(() => {
		if (activeOptionId) document.getElementById(activeOptionId)?.scrollIntoView?.({ block: 'nearest' });
	});

	function onkeydown(event: KeyboardEvent) {
		if (controller.keydown(event)) event.preventDefault();
	}
</script>

<div class="relative">
	<Search size={15} class="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-app-muted" />
	<input
		{id}
		class="{inputClass} bg-app-card pl-9 {$controller.selected ? 'pr-9' : ''}"
		type="text"
		role="combobox"
		autocomplete="off"
		autocapitalize="none"
		spellcheck="false"
		maxlength={MAX_QUERY_LENGTH}
		aria-autocomplete="list"
		aria-expanded={expanded}
		aria-controls={listboxId}
		aria-activedescendant={activeOptionId}
		aria-describedby={describedby}
		{placeholder}
		{disabled}
		value={$controller.query}
		oninput={(event) => controller.input(event.currentTarget.value)}
		{onkeydown}
		onfocus={() => controller.focus()}
		onblur={() => controller.blur()}
	/>
	{#if $controller.selected}
		<CircleCheck size={16} class="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-app-success" />
	{/if}

	<div
		class="absolute inset-x-0 top-[calc(100%+0.375rem)] z-20 {$controller.open
			? 'rounded-lg border border-app-line bg-app-raised p-1.5 shadow-[var(--shadow-popover)]'
			: ''}"
	>
		<!-- Pressing an option must not blur the input first, or the popup would close under the click. -->
		<ul
			id={listboxId}
			class="m-0 grid max-h-72 list-none gap-0.5 overflow-y-auto p-0"
			role="listbox"
			aria-label="Matching people"
			hidden={!expanded}
			onmousedown={(event) => event.preventDefault()}
		>
			{#each $controller.results as candidate, index (candidate.user_id)}
				{@const name = displayNameOf(candidate)}
				<!--
					Options never take focus, so the global :focus-visible ring never
					reaches them: the active one, where Enter lands, draws that ring itself.
				-->
				<!-- svelte-ignore a11y_click_events_have_key_events -- options are never focused: the keyboard reaches them through the input's aria-activedescendant -->
				<li
					id={optionId(index)}
					class="flex cursor-pointer items-center gap-2.5 rounded-md px-2.5 py-2 {index === $controller.activeIndex
						? 'bg-app-sidebar-hover outline-2 -outline-offset-2 outline-[var(--focus)]'
						: ''}"
					role="option"
					aria-selected={index === $controller.activeIndex}
					aria-label={candidateOptionLabel(candidate)}
					onpointermove={() => controller.activate(index)}
					onclick={() => controller.select(index)}
				>
					<span
						class="grid size-8 shrink-0 place-items-center rounded-full border border-app-line bg-app-card text-[11px] font-semibold text-app-muted"
						aria-hidden="true">{personInitials(candidate)}</span
					>
					<span class="min-w-0 flex-1">
						<span class="block truncate text-sm font-medium text-app-fg">{name ?? `@${candidate.username}`}</span>
						<span class="block truncate text-xs text-app-muted">
							{name ? `@${candidate.username} · ${candidate.email_hint}` : candidate.email_hint}
						</span>
					</span>
				</li>
			{/each}
		</ul>

		<!-- Always in the page, so what it says is announced when it changes. "Searching" is shown, not announced. -->
		<div role="status">
			{#if $controller.open}
				{#if $controller.status === 'loading'}
					<p class="m-0 flex items-center gap-2 px-2.5 py-2 text-sm text-app-muted" aria-hidden="true">
						<LoaderCircle size={15} class="shrink-0 animate-spin" />Searching...
					</p>
				{:else if $controller.status === 'empty'}
					<div class="px-2.5 py-2">
						<p class="m-0 text-sm font-medium text-app-fg">No matching people</p>
						<p class="m-0 mt-0.5 text-xs text-app-muted">{noMatchHint($controller.query)}</p>
					</div>
				{:else if $controller.status === 'error'}
					<p class="m-0 flex items-start gap-2 px-2.5 py-2 text-sm text-app-danger">
						<CircleAlert size={15} class="mt-0.5 shrink-0" />{$controller.error}
					</p>
				{:else if $controller.status === 'results'}
					{@const count = $controller.results.length}
					<!-- A full list may have been cut short, so that is shown as well as announced. -->
					<p
						class={mayHaveMoreMatches(count)
							? 'm-0 mt-1 border-t border-app-line px-2.5 pb-1 pt-2 text-xs text-app-muted'
							: 'visually-hidden'}
					>
						{resultCountText(count)}
					</p>
				{/if}
			{/if}
		</div>
	</div>
</div>
