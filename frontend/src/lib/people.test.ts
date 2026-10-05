import { describe, expect, it } from 'vitest';

import { displayNameOf, personInitials, personLabel } from './people';

describe('displayNameOf', () => {
	it('returns the trimmed display name, or null when there is none', () => {
		expect(displayNameOf({ username: 'ada', display_name: '  Ada Lovelace ' })).toBe('Ada Lovelace');
		expect(displayNameOf({ username: 'ada', display_name: '   ' })).toBeNull();
		expect(displayNameOf({ username: 'ada', display_name: null })).toBeNull();
		expect(displayNameOf({ username: 'ada' })).toBeNull();
	});
});

describe('personLabel', () => {
	it('pairs a display name with the unique username', () => {
		expect(personLabel({ username: 'ada', display_name: 'Ada Lovelace' })).toBe('Ada Lovelace (ada)');
	});

	it('is just the username without a display name', () => {
		expect(personLabel({ username: 'ada', display_name: null })).toBe('ada');
	});
});

describe('personInitials', () => {
	it('uses the first and last word of a display name', () => {
		expect(personInitials({ username: 'x', display_name: 'Ada Lovelace' })).toBe('AL');
		expect(personInitials({ username: 'x', display_name: 'ada king  lovelace' })).toBe('AL');
	});

	it('uses the first two letters of a one-word display name', () => {
		expect(personInitials({ username: 'x', display_name: 'Ada' })).toBe('AD');
	});

	it('falls back to the username without a display name', () => {
		expect(personInitials({ username: 'grace', display_name: null })).toBe('GR');
		expect(personInitials({ username: 'grace', display_name: '  ' })).toBe('GR');
	});

	it('never splits a character made of two UTF-16 units', () => {
		expect(personInitials({ username: 'x', display_name: '😀 Smile' })).toBe('😀S');
		expect(personInitials({ username: '😀x', display_name: null })).toBe('😀X');
	});
});
