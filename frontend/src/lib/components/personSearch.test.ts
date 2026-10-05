import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, type MemberCandidate } from '$lib/api';
import {
	INITIAL_PERSON_SEARCH,
	MAX_CANDIDATES,
	SEARCH_DEBOUNCE_MS,
	candidateOptionLabel,
	createPersonSearch,
	isListboxOpen,
	mayHaveMoreMatches,
	noMatchHint,
	resultCountText,
	searchableQuery,
	type PersonSearchState
} from './personSearch';

function candidate(username: string, display_name: string | null = null): MemberCandidate {
	return { user_id: `id-${username}`, username, display_name, email_hint: `${username[0]}•••@example.com` };
}

const ada = candidate('ada', 'Ada Lovelace');
const adam = candidate('adam');
const adelaide = candidate('adelaide', 'Adelaide Example');

type Request = {
	query: string;
	signal: AbortSignal;
	resolve: (results: MemberCandidate[]) => void;
	reject: (error: unknown) => void;
};

// Every search stays pending until the test answers it, so the order in which
// answers arrive is entirely up to the test.
function harness() {
	const requests: Request[] = [];
	const search = vi.fn(
		(query: string, signal: AbortSignal) =>
			new Promise<MemberCandidate[]>((resolve, reject) => requests.push({ query, signal, resolve, reject }))
	);
	const people = createPersonSearch({ search });
	return { people, search, requests, state: () => get(people) };
}

// Lets the awaited search inside the controller resume and commit.
async function settle() {
	for (let i = 0; i < 5; i++) await Promise.resolve();
}

// Types `text`, waits out the debounce, and answers the search with `results`.
async function searchFor(h: ReturnType<typeof harness>, text: string, results: MemberCandidate[]) {
	h.people.input(text);
	vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
	h.requests.at(-1)!.resolve(results);
	await settle();
}

beforeEach(() => {
	vi.useFakeTimers();
});

afterEach(() => {
	vi.useRealTimers();
});

describe('searchableQuery', () => {
	it('trims, then requires 2 to 255 characters', () => {
		expect(searchableQuery('  ad  ')).toBe('ad');
		expect(searchableQuery('a')).toBeNull();
		expect(searchableQuery('  a  ')).toBeNull();
		expect(searchableQuery('   ')).toBeNull();
		expect(searchableQuery('x'.repeat(255))).toBe('x'.repeat(255));
		expect(searchableQuery('x'.repeat(256))).toBeNull();
	});

	it('counts characters the way the backend does, not UTF-16 units', () => {
		expect(searchableQuery('😀')).toBeNull();
		expect(searchableQuery('😀😀')).toBe('😀😀');
		expect(searchableQuery('😀'.repeat(255))).toBe('😀'.repeat(255));
	});
});

describe('typing and debouncing', () => {
	it('starts idle and closed, with no one chosen', () => {
		expect(harness().state()).toEqual(INITIAL_PERSON_SEARCH);
	});

	it('shows loading as soon as the query is searchable, before any request', () => {
		const h = harness();
		h.people.input('ad');

		expect(h.state()).toMatchObject({ query: 'ad', status: 'loading', pending: true, open: true, results: [] });
		expect(h.search).not.toHaveBeenCalled();
	});

	it('searches once, after a pause in typing, with the trimmed query', () => {
		const h = harness();
		h.people.input('ad');
		vi.advanceTimersByTime(200);
		h.people.input('ada ');

		// Each edit restarts the wait.
		vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS - 1);
		expect(h.search).not.toHaveBeenCalled();

		vi.advanceTimersByTime(1);
		expect(h.search).toHaveBeenCalledTimes(1);
		expect(h.search).toHaveBeenCalledWith('ada', expect.any(AbortSignal));
		expect(h.state().query).toBe('ada ');
	});

	it('never searches below two characters, and stays closed', () => {
		const h = harness();
		h.people.input('a');
		h.people.input('  a  ');
		vi.advanceTimersByTime(10 * SEARCH_DEBOUNCE_MS);

		expect(h.search).not.toHaveBeenCalled();
		expect(h.state()).toEqual({ ...INITIAL_PERSON_SEARCH, query: '  a  ' });
	});

	it('cancels a pending search when the query becomes too short again', () => {
		const h = harness();
		h.people.input('ada');
		h.people.input('a');
		vi.advanceTimersByTime(10 * SEARCH_DEBOUNCE_MS);

		expect(h.search).not.toHaveBeenCalled();
		expect(h.state().status).toBe('idle');
	});

	it('keeps what is on screen when only surrounding whitespace changes', async () => {
		const h = harness();
		await searchFor(h, 'ada', [ada]);

		h.people.input('ada  ');
		vi.advanceTimersByTime(10 * SEARCH_DEBOUNCE_MS);

		expect(h.search).toHaveBeenCalledTimes(1);
		expect(h.state()).toMatchObject({ query: 'ada  ', status: 'results', results: [ada], open: true });
	});
});

describe('results, empty, and error states', () => {
	it('lists the people found, with no option active yet', async () => {
		const h = harness();
		await searchFor(h, 'ad', [ada, adam]);

		expect(h.state()).toMatchObject({ status: 'results', results: [ada, adam], activeIndex: -1, open: true, error: null });
		expect(isListboxOpen(h.state())).toBe(true);
	});

	it('reports an empty result as its own state, without opening the listbox', async () => {
		const h = harness();
		await searchFor(h, 'zz', []);

		expect(h.state()).toMatchObject({ status: 'empty', results: [], open: true });
		expect(isListboxOpen(h.state())).toBe(false);
	});

	it('reports the backend detail when the search fails', async () => {
		const h = harness();
		h.people.input('ada');
		vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
		h.requests[0].reject(new ApiError(403, 'Only an Owner can add members'));
		await settle();

		expect(h.state()).toMatchObject({ status: 'error', error: 'Only an Owner can add members', results: [], open: true });
		expect(isListboxOpen(h.state())).toBe(false);
	});

	it('falls back to a generic message for a network failure', async () => {
		const h = harness();
		h.people.input('ada');
		vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
		h.requests[0].reject(new TypeError('Failed to fetch'));
		await settle();

		expect(h.state()).toMatchObject({ status: 'error', error: 'Unable to search for people.' });
	});

	it('retries a failed search on any edit, even whitespace', async () => {
		const h = harness();
		h.people.input('ada');
		vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
		h.requests[0].reject(new TypeError('Failed to fetch'));
		await settle();

		h.people.input('ada ');
		expect(h.state()).toMatchObject({ status: 'loading', error: null });
		vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
		expect(h.search).toHaveBeenCalledTimes(2);
	});
});

describe('stale responses', () => {
	it('ignores a slower answer to an earlier query', async () => {
		const h = harness();
		h.people.input('ad');
		vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
		h.people.input('ada');
		vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
		expect(h.requests.map((request) => request.query)).toEqual(['ad', 'ada']);

		h.requests[1].resolve([ada]);
		await settle();
		h.requests[0].resolve([ada, adam, adelaide]);
		await settle();

		expect(h.state()).toMatchObject({ status: 'results', results: [ada] });
	});

	it('ignores an answer that lands while a newer query is still waiting to be sent', async () => {
		const h = harness();
		h.people.input('ad');
		vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
		h.people.input('adx');

		h.requests[0].resolve([ada, adam]);
		await settle();

		expect(h.state()).toMatchObject({ query: 'adx', status: 'loading', results: [] });
	});

	it('ignores a failure of a superseded search', async () => {
		const h = harness();
		h.people.input('ad');
		vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
		h.people.input('ada');

		h.requests[0].reject(new ApiError(500, 'Internal Server Error'));
		await settle();

		expect(h.state()).toMatchObject({ status: 'loading', error: null });
	});

	it('aborts the request that a newer query supersedes, and only that one', () => {
		const h = harness();
		h.people.input('ad');
		vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
		expect(h.requests[0].signal.aborted).toBe(false);

		h.people.input('ada');
		vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);

		expect(h.requests[0].signal.aborted).toBe(true);
		expect(h.requests[1].signal.aborted).toBe(false);
	});

	it('ignores an answer that arrives after a reset', async () => {
		const h = harness();
		h.people.input('ada');
		vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
		h.people.reset();

		expect(h.requests[0].signal.aborted).toBe(true);
		h.requests[0].resolve([ada]);
		await settle();

		expect(h.state()).toEqual(INITIAL_PERSON_SEARCH);
	});

	it('never sends a search that was still waiting when the component went away', () => {
		const h = harness();
		h.people.input('ada');
		h.people.destroy();
		vi.advanceTimersByTime(10 * SEARCH_DEBOUNCE_MS);

		expect(h.search).not.toHaveBeenCalled();
	});
});

// The listbox must not collapse to "Searching..." and back on every keystroke:
// the people already listed stay until the next answer replaces them.
describe('typing on while people are listed', () => {
	async function listed() {
		const h = harness();
		await searchFor(h, 'ad', [ada, adam, adelaide]);
		return h;
	}

	it('keeps them listed, and the listbox open, while the next search is pending', async () => {
		const h = await listed();
		h.people.keydown({ key: 'ArrowDown' });

		h.people.input('ada');

		// Typing moves the keyboard back to the text, so no option is active.
		expect(h.state()).toMatchObject({
			query: 'ada',
			status: 'results',
			results: [ada, adam, adelaide],
			pending: true,
			activeIndex: -1,
			open: true
		});
		expect(isListboxOpen(h.state())).toBe(true);

		vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
		expect(isListboxOpen(h.state())).toBe(true);
	});

	it('replaces them with the next answer once it lands', async () => {
		const h = await listed();
		await searchFor(h, 'ada', [ada]);

		expect(h.state()).toMatchObject({ query: 'ada', status: 'results', results: [ada], pending: false, activeIndex: -1 });
	});

	it('replaces them with an empty answer, closing the listbox', async () => {
		const h = await listed();
		await searchFor(h, 'adz', []);

		expect(h.state()).toMatchObject({ status: 'empty', results: [], pending: false, open: true });
		expect(isListboxOpen(h.state())).toBe(false);
	});

	it('replaces them with an error, closing the listbox', async () => {
		const h = await listed();
		h.people.input('ada');
		vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
		h.requests.at(-1)!.reject(new TypeError('Failed to fetch'));
		await settle();

		expect(h.state()).toMatchObject({ status: 'error', results: [], pending: false, error: 'Unable to search for people.' });
		expect(isListboxOpen(h.state())).toBe(false);
	});

	it('never lets the answer to a query typed over replace them', async () => {
		const h = await listed();
		h.people.input('ada');
		vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
		h.people.input('adam');

		h.requests.at(-1)!.resolve([ada]);
		await settle();

		expect(h.state()).toMatchObject({ query: 'adam', status: 'results', results: [ada, adam, adelaide], pending: true });
	});

	it('lets the arrow keys reach them meanwhile, and Enter choose one', async () => {
		const h = await listed();
		h.people.input('ada');

		expect(h.people.keydown({ key: 'ArrowDown' })).toBe(true);
		expect(h.people.keydown({ key: 'ArrowDown' })).toBe(true);
		expect(h.people.keydown({ key: 'Enter' })).toBe(true);

		expect(h.state()).toMatchObject({ selected: adam, query: 'adam', pending: false, open: false });
		// Choosing someone stops the search for what was typed before: only
		// the first search, for "ad", was ever sent.
		vi.advanceTimersByTime(10 * SEARCH_DEBOUNCE_MS);
		expect(h.search).toHaveBeenCalledTimes(1);
	});

	it('keeps the person the keyboard is on active if the next answer still lists them', async () => {
		const h = await listed();
		h.people.input('ada');
		h.people.keydown({ key: 'ArrowDown' });
		h.people.keydown({ key: 'ArrowDown' });
		expect(h.state().results[h.state().activeIndex]).toBe(adam);

		vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
		h.requests.at(-1)!.resolve([adelaide, adam]);
		await settle();

		expect(h.state()).toMatchObject({ results: [adelaide, adam], activeIndex: 1 });
	});

	it('leaves no option active if the next answer drops that person', async () => {
		const h = await listed();
		h.people.input('ada');
		h.people.keydown({ key: 'ArrowDown' });

		vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
		h.requests.at(-1)!.resolve([adam, adelaide]);
		await settle();

		expect(h.state()).toMatchObject({ results: [adam, adelaide], activeIndex: -1 });
	});

	it('still shows loading when nothing was listed: after an empty answer', async () => {
		const h = harness();
		await searchFor(h, 'zz', []);

		h.people.input('zzz');
		expect(h.state()).toMatchObject({ status: 'loading', results: [], pending: true });
	});

	it('clears them when the query becomes too short to search', async () => {
		const h = await listed();
		h.people.input('a');

		expect(h.state()).toEqual({ ...INITIAL_PERSON_SEARCH, query: 'a' });
	});
});

describe('keyboard', () => {
	async function withResults() {
		const h = harness();
		await searchFor(h, 'ad', [ada, adam, adelaide]);
		return h;
	}

	const key = (name: string) => ({ key: name });

	it('ArrowDown moves through the options and wraps from the last to the first', async () => {
		const h = await withResults();

		expect(h.people.keydown(key('ArrowDown'))).toBe(true);
		expect(h.state().activeIndex).toBe(0);
		h.people.keydown(key('ArrowDown'));
		h.people.keydown(key('ArrowDown'));
		expect(h.state().activeIndex).toBe(2);
		h.people.keydown(key('ArrowDown'));
		expect(h.state().activeIndex).toBe(0);
	});

	it('ArrowUp from the input goes to the last option and wraps from the first to the last', async () => {
		const h = await withResults();

		expect(h.people.keydown(key('ArrowUp'))).toBe(true);
		expect(h.state().activeIndex).toBe(2);
		h.people.keydown(key('ArrowUp'));
		h.people.keydown(key('ArrowUp'));
		expect(h.state().activeIndex).toBe(0);
		h.people.keydown(key('ArrowUp'));
		expect(h.state().activeIndex).toBe(2);
	});

	it('Enter chooses the active option and closes the popup', async () => {
		const h = await withResults();
		h.people.keydown(key('ArrowDown'));

		expect(h.people.keydown(key('Enter'))).toBe(true);
		expect(h.state()).toEqual<PersonSearchState>({
			...INITIAL_PERSON_SEARCH,
			query: 'Ada Lovelace (ada)',
			selected: ada
		});
	});

	it('leaves Enter to the form when no option is active', async () => {
		const h = await withResults();

		expect(h.people.keydown(key('Enter'))).toBe(false);
		expect(h.state().selected).toBeNull();
	});

	it('Escape first dismisses the popup, then clears the input', async () => {
		const h = await withResults();
		h.people.keydown(key('ArrowDown'));

		expect(h.people.keydown(key('Escape'))).toBe(true);
		expect(h.state()).toMatchObject({ open: false, activeIndex: -1, query: 'ad', results: [ada, adam, adelaide] });

		expect(h.people.keydown(key('Escape'))).toBe(true);
		expect(h.state()).toEqual(INITIAL_PERSON_SEARCH);

		expect(h.people.keydown(key('Escape'))).toBe(false);
	});

	it('Escape also dismisses an empty-result message', async () => {
		const h = harness();
		await searchFor(h, 'zz', []);

		expect(h.people.keydown(key('Escape'))).toBe(true);
		expect(h.state()).toMatchObject({ open: false, status: 'empty', query: 'zz' });
	});

	it('ArrowDown reopens a dismissed popup on the first option', async () => {
		const h = await withResults();
		h.people.keydown(key('ArrowDown'));
		h.people.keydown(key('ArrowDown'));
		h.people.keydown(key('Escape'));

		expect(h.people.keydown(key('ArrowDown'))).toBe(true);
		expect(h.state()).toMatchObject({ open: true, activeIndex: 0 });
	});

	it('leaves the arrow keys alone while there are no options', () => {
		const h = harness();
		h.people.input('ada');

		expect(h.people.keydown(key('ArrowDown'))).toBe(false);
		expect(h.people.keydown(key('ArrowUp'))).toBe(false);
		expect(h.state().activeIndex).toBe(-1);
	});

	it('ignores modified keys, IME composition, and every other key', async () => {
		const h = await withResults();
		h.people.keydown(key('ArrowDown'));

		expect(h.people.keydown({ key: 'Enter', isComposing: true })).toBe(false);
		expect(h.people.keydown({ key: 'ArrowDown', shiftKey: true })).toBe(false);
		expect(h.people.keydown({ key: 'ArrowDown', altKey: true })).toBe(false);
		expect(h.people.keydown({ key: 'ArrowUp', ctrlKey: true })).toBe(false);
		expect(h.people.keydown({ key: 'Enter', metaKey: true })).toBe(false);
		expect(h.people.keydown(key('Home'))).toBe(false);
		expect(h.people.keydown(key('a'))).toBe(false);
		expect(h.state()).toMatchObject({ activeIndex: 0, selected: null, open: true });
	});
});

describe('choosing a person', () => {
	it('fills the input with the display name and username, or just the username', async () => {
		const h = harness();
		await searchFor(h, 'ad', [ada, adam]);
		h.people.select(1);

		expect(h.state()).toMatchObject({ query: 'adam', selected: adam, status: 'idle', open: false, results: [] });
	});

	it('editing the text afterwards drops the choice and searches again', async () => {
		const h = harness();
		await searchFor(h, 'ad', [ada]);
		h.people.select(0);

		h.people.input('grace');
		expect(h.state()).toMatchObject({ query: 'grace', selected: null, status: 'loading' });
		vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
		expect(h.search).toHaveBeenLastCalledWith('grace', expect.any(AbortSignal));
	});

	it('editing the chosen text down to nothing drops the choice too', async () => {
		const h = harness();
		await searchFor(h, 'ad', [ada]);
		h.people.select(0);

		h.people.input('');
		expect(h.state()).toEqual(INITIAL_PERSON_SEARCH);
	});

	it('ignores an index outside the results', async () => {
		const h = harness();
		await searchFor(h, 'ad', [ada]);
		h.people.select(3);

		expect(h.state()).toMatchObject({ selected: null, status: 'results' });
	});

	it('reset clears the query, the results, and the choice', async () => {
		const h = harness();
		await searchFor(h, 'ad', [ada]);
		h.people.select(0);
		h.people.reset();

		expect(h.state()).toEqual(INITIAL_PERSON_SEARCH);
	});
});

describe('pointer and focus', () => {
	it('activate points at an option without choosing it', async () => {
		const h = harness();
		await searchFor(h, 'ad', [ada, adam]);

		h.people.activate(1);
		expect(h.state()).toMatchObject({ activeIndex: 1, selected: null });

		h.people.activate(-1);
		h.people.activate(2);
		expect(h.state().activeIndex).toBe(1);
	});

	it('activate does nothing while the popup is closed', async () => {
		const h = harness();
		await searchFor(h, 'ad', [ada, adam]);
		h.people.blur();

		h.people.activate(1);
		expect(h.state().activeIndex).toBe(-1);
	});

	it('blur closes the popup and forgets the active option', async () => {
		const h = harness();
		await searchFor(h, 'ad', [ada, adam]);
		h.people.keydown({ key: 'ArrowDown' });

		h.people.blur();
		expect(h.state()).toMatchObject({ open: false, activeIndex: -1, status: 'results', results: [ada, adam] });
	});

	it('results that arrive after blur wait, closed, until focus returns', async () => {
		const h = harness();
		h.people.input('ada');
		h.people.blur();
		vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
		h.requests[0].resolve([ada]);
		await settle();

		expect(h.state()).toMatchObject({ status: 'results', open: false });
		expect(isListboxOpen(h.state())).toBe(false);

		h.people.focus();
		expect(h.state().open).toBe(true);
		expect(isListboxOpen(h.state())).toBe(true);
	});

	it('focus opens nothing while there is nothing to show', () => {
		const h = harness();
		h.people.focus();
		expect(h.state().open).toBe(false);

		h.people.input('a');
		h.people.focus();
		expect(h.state().open).toBe(false);
	});
});

describe('presentation helpers', () => {
	it('names each option by everything it shows', () => {
		expect(candidateOptionLabel(ada)).toBe('Ada Lovelace, @ada, a•••@example.com');
		expect(candidateOptionLabel(adam)).toBe('@adam, a•••@example.com');
		expect(candidateOptionLabel({ ...ada, display_name: '   ' })).toBe('@ada, a•••@example.com');
	});

	it('counts the people found', () => {
		expect(resultCountText(1)).toBe('1 person found');
		expect(resultCountText(4)).toBe('4 people found');
		expect(resultCountText(MAX_CANDIDATES - 1)).toBe('9 people found');
	});

	// The backend returns at most 10 people and never says how many matched in
	// all, so a full list may have been cut short: the person the Owner wants
	// must not look like they do not exist.
	it('says a full list may have been cut short, and how to narrow it', () => {
		expect(MAX_CANDIDATES).toBe(10);
		expect(mayHaveMoreMatches(MAX_CANDIDATES - 1)).toBe(false);
		expect(mayHaveMoreMatches(MAX_CANDIDATES)).toBe(true);
		expect(resultCountText(MAX_CANDIDATES)).toBe('Showing the first 10 matches. Keep typing to narrow the search.');
	});

	it('explains that email search needs a complete address only for email-like queries', () => {
		expect(noMatchHint('ada@example')).toMatch(/complete address/);
		expect(noMatchHint('ada')).not.toMatch(/address/);
		expect(noMatchHint('ada')).toMatch(/Existing members are not listed/);
	});
});
