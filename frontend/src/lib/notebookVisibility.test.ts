import { describe, expect, it } from 'vitest';

import { VISIBILITY_LABELS, VISIBILITY_ORDER } from './notebookVisibility';

describe('notebook visibility labels', () => {
	it('names every visibility value with no leftover draft/publish terminology', () => {
		expect(VISIBILITY_LABELS).toEqual({ private: 'Private', unlisted: 'Unlisted', public: 'Public' });
	});

	it('orders the options Private, Unlisted, Public for the select control', () => {
		expect(VISIBILITY_ORDER).toEqual(['private', 'unlisted', 'public']);
	});
});
