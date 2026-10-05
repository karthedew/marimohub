// The add-member Person combobox's behavior: debouncing, the stale-response
// guard, and the WAI-ARIA combobox keyboard model. Kept out of the component
// so all of it is unit tested without a DOM; `PersonSearch.svelte` only
// renders this state and forwards input, focus, and key events to it.
import { writable, type Readable } from 'svelte/store';

import { ApiError, type MemberCandidate } from '$lib/api';
import { displayNameOf, personLabel } from '$lib/people';

// The backend's bounds for `q` once trimmed, counted in characters (code
// points) as the backend counts them. A query outside them could only earn a
// 422, so it never leaves the browser.
export const MIN_QUERY_LENGTH = 2;
export const MAX_QUERY_LENGTH = 255;
// The most people one search returns. The backend never says how many matched
// in all, so a list this long may have been cut short.
export const MAX_CANDIDATES = 10;
export const SEARCH_DEBOUNCE_MS = 250;

export type PersonSearchStatus =
	// Too short to search yet, or a person was just chosen.
	| 'idle'
	// A search is pending and no one is listed meanwhile.
	| 'loading'
	| 'results'
	| 'empty'
	| 'error';

export type PersonSearchState = {
	// Exactly what the input shows.
	query: string;
	status: PersonSearchStatus;
	// While `pending`, still the previous query's answer: the people listed
	// stay listed until the next answer replaces them, instead of the listbox
	// collapsing on every keystroke.
	results: MemberCandidate[];
	// A search for the current query is waiting out the debounce or in flight.
	pending: boolean;
	// The option the keyboard (or pointer) is on; -1 for none.
	activeIndex: number;
	// Whether the popup shows. Never true while `status` is `idle`.
	open: boolean;
	selected: MemberCandidate | null;
	error: string | null;
};

export type PersonSearchFn = (query: string, signal: AbortSignal) => Promise<MemberCandidate[]>;

export type PersonSearchOptions = {
	search: PersonSearchFn;
	debounceMs?: number;
};

// The parts of a KeyboardEvent the combobox reads.
export type ComboboxKey = Pick<KeyboardEvent, 'key'> &
	Partial<Pick<KeyboardEvent, 'altKey' | 'ctrlKey' | 'metaKey' | 'shiftKey' | 'isComposing'>>;

export type PersonSearchController = Readable<PersonSearchState> & {
	// The input's text changed (typing, paste, cut).
	input: (text: string) => void;
	// Returns whether the key was handled, i.e. its default should be prevented.
	keydown: (event: ComboboxKey) => boolean;
	// Choose the option at `index` (Enter on it, or a click).
	select: (index: number) => void;
	// Point at the option at `index` without choosing it (pointer hover).
	activate: (index: number) => void;
	focus: () => void;
	blur: () => void;
	// Back to an empty input with no one chosen, e.g. after a successful add.
	reset: () => void;
	// Stops any pending or in-flight search; call when the component goes away.
	destroy: () => void;
};

export const INITIAL_PERSON_SEARCH: PersonSearchState = {
	query: '',
	status: 'idle',
	results: [],
	pending: false,
	activeIndex: -1,
	open: false,
	selected: null,
	error: null
};

// The trimmed query to send, or null when it is outside the backend's bounds.
export function searchableQuery(text: string): string | null {
	const query = text.trim();
	const length = Array.from(query).length;
	return length >= MIN_QUERY_LENGTH && length <= MAX_QUERY_LENGTH ? query : null;
}

export function searchErrorMessage(caught: unknown): string {
	if (caught instanceof ApiError && caught.detail) return caught.detail;
	return 'Unable to search for people.';
}

export function createPersonSearch({ search, debounceMs = SEARCH_DEBOUNCE_MS }: PersonSearchOptions): PersonSearchController {
	let state = INITIAL_PERSON_SEARCH;
	const store = writable(state);

	let timer: ReturnType<typeof setTimeout> | null = null;
	let inflight: AbortController | null = null;
	// Bumped whenever a search is superseded. A response only lands while its
	// ticket still matches, so a slow answer to an older query can never
	// overwrite the answer to a newer one, or reappear after a reset.
	let generation = 0;
	// The trimmed query last searched for: the one the results answer, or will
	// answer once a pending search lands.
	let searched: string | null = null;

	function commit(next: Partial<PersonSearchState>) {
		state = { ...state, ...next };
		store.set(state);
	}

	function cancel() {
		generation += 1;
		if (timer !== null) {
			clearTimeout(timer);
			timer = null;
		}
		inflight?.abort();
		inflight = null;
	}

	async function run(query: string, ticket: number) {
		timer = null;
		const controller = new AbortController();
		inflight = controller;
		try {
			const results = await search(query, controller.signal);
			if (ticket !== generation) return;
			// Someone the keyboard reached in the previous list stays active if
			// this answer lists them too; option ids follow the index, so theirs
			// changes with it and is announced again.
			const active = state.results[state.activeIndex];
			const activeIndex = active ? results.findIndex((candidate) => candidate.user_id === active.user_id) : -1;
			commit({ status: results.length > 0 ? 'results' : 'empty', results, pending: false, activeIndex, error: null });
		} catch (caught) {
			if (ticket !== generation) return;
			commit({ status: 'error', results: [], pending: false, activeIndex: -1, error: searchErrorMessage(caught) });
		} finally {
			if (inflight === controller) inflight = null;
		}
	}

	function input(text: string) {
		const query = searchableQuery(text);

		// Only surrounding whitespace changed: this query is answered already,
		// or about to be, so keep that rather than start the search over. A
		// failed search is the exception — any edit retries it.
		if (query !== null && query === searched && state.selected === null && state.status !== 'error') {
			commit({ query: text, open: true });
			return;
		}

		// Any other edit replaces whatever was chosen: the text no longer names them.
		cancel();
		if (query === null) {
			searched = null;
			commit({ ...INITIAL_PERSON_SEARCH, query: text });
			return;
		}

		searched = query;
		if (state.status === 'results') {
			// The people listed stay listed, and the listbox open, until the
			// answer lands. Typing moves the keyboard back to the text, so no
			// option stays active.
			commit({ query: text, pending: true, activeIndex: -1, open: true });
		} else {
			commit({ ...INITIAL_PERSON_SEARCH, query: text, status: 'loading', pending: true, open: true });
		}
		const ticket = generation;
		timer = setTimeout(() => void run(query, ticket), debounceMs);
	}

	function select(index: number) {
		const candidate = state.results[index];
		if (!candidate) return;
		cancel();
		searched = null;
		commit({ ...INITIAL_PERSON_SEARCH, query: personLabel(candidate), selected: candidate });
	}

	function activate(index: number) {
		if (!state.open || index === state.activeIndex || index < 0 || index >= state.results.length) return;
		commit({ activeIndex: index });
	}

	function move(step: 1 | -1) {
		const count = state.results.length;
		if (count === 0) return false;
		// From the input (or a closed popup) the first step lands on the first
		// option going down, the last going up; after that it wraps around.
		const from = state.open ? state.activeIndex : -1;
		const index = from < 0 ? (step === 1 ? 0 : count - 1) : (from + step + count) % count;
		commit({ open: true, activeIndex: index });
		return true;
	}

	function keydown(event: ComboboxKey): boolean {
		// Modified keys keep their text-editing meaning, and Enter during IME
		// composition commits the composition, never an option.
		if (event.isComposing || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return false;

		switch (event.key) {
			case 'ArrowDown':
				return move(1);
			case 'ArrowUp':
				return move(-1);
			case 'Enter':
				if (!state.open || state.activeIndex < 0) return false;
				select(state.activeIndex);
				return true;
			case 'Escape':
				// First Escape dismisses the popup; with it already closed, clears the input.
				if (state.open) {
					commit({ open: false, activeIndex: -1 });
					return true;
				}
				if (state.query !== '') {
					reset();
					return true;
				}
				return false;
			default:
				return false;
		}
	}

	function focus() {
		if (!state.open && state.status !== 'idle') commit({ open: true });
	}

	function blur() {
		if (state.open || state.activeIndex !== -1) commit({ open: false, activeIndex: -1 });
	}

	function reset() {
		cancel();
		searched = null;
		commit(INITIAL_PERSON_SEARCH);
	}

	return {
		subscribe: store.subscribe,
		input,
		keydown,
		select,
		activate,
		focus,
		blur,
		reset,
		destroy: cancel
	};
}

// The listbox shows only while there are options in it; loading, empty, and
// error states are messages beside it, not options.
export function isListboxOpen(state: PersonSearchState): boolean {
	return state.open && state.status === 'results' && state.results.length > 0;
}

// Each option's accessible name: everything the option shows, in reading order.
export function candidateOptionLabel(candidate: MemberCandidate): string {
	const name = displayNameOf(candidate);
	return [name, `@${candidate.username}`, candidate.email_hint].filter(Boolean).join(', ');
}

// A full list may have been cut short at the backend's cap.
export function mayHaveMoreMatches(count: number): boolean {
	return count >= MAX_CANDIDATES;
}

// What the status line says about the people listed. A full list must not
// pass for every match, or the person the Owner wants could seem not to exist.
export function resultCountText(count: number): string {
	if (mayHaveMoreMatches(count)) {
		return `Showing the first ${MAX_CANDIDATES} matches. Keep typing to narrow the search.`;
	}
	return `${count} ${count === 1 ? 'person' : 'people'} found`;
}

// Why a search can come back empty even though the person exists: email
// matches only a complete address (so nobody can list everyone at a domain),
// and people already in the Workspace are never offered again.
export function noMatchHint(query: string): string {
	return query.includes('@')
		? 'Email search only matches a complete address. Existing members are not listed.'
		: 'Existing members are not listed.';
}
