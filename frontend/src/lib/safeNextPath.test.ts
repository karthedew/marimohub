import { describe, expect, it } from 'vitest';

import { safeNextPath } from './safeNextPath';

describe('safeNextPath', () => {
	it('accepts a same-origin absolute path', () => {
		expect(safeNextPath('/notebooks/123/edit')).toBe('/notebooks/123/edit');
	});

	it('preserves a query string on the path', () => {
		expect(safeNextPath('/discover?tags=ml')).toBe('/discover?tags=ml');
	});

	it('falls back for missing input', () => {
		expect(safeNextPath(null)).toBe('/');
		expect(safeNextPath(undefined)).toBe('/');
		expect(safeNextPath('')).toBe('/');
	});

	it('rejects a protocol-relative URL, which browsers treat as an open redirect', () => {
		expect(safeNextPath('//evil.example.com')).toBe('/');
	});

	it('rejects an absolute URL to another origin', () => {
		expect(safeNextPath('https://evil.example.com')).toBe('/');
	});

	it('rejects a value that is not a path at all', () => {
		expect(safeNextPath('javascript:alert(1)')).toBe('/');
	});

	it('honors a caller-supplied fallback', () => {
		expect(safeNextPath(null, '/discover')).toBe('/discover');
	});
});
