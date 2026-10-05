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

	it('keeps a fragment and percent-encoded characters on a same-origin path', () => {
		expect(safeNextPath('/notebooks/123#cell-4')).toBe('/notebooks/123#cell-4');
		expect(safeNextPath('/discover?q=%2F%2Fnot-a-host')).toBe('/discover?q=%2F%2Fnot-a-host');
	});

	// Each of these starts with a single `/`, yet the URL parser resolves it
	// to another host: it reads `\` as `/` and strips tabs and newlines.
	it.each([
		['/\\evil.example.com'],
		['/\t/evil.example.com'],
		['/\n/evil.example.com'],
		['/\r/evil.example.com'],
		['/\\/evil.example.com'],
		['/\t\\evil.example.com']
	])('rejects %j, which a URL parser resolves to another origin', (value) => {
		expect(new URL(value, 'http://app.example').origin).not.toBe('http://app.example');
		expect(safeNextPath(value)).toBe('/');
	});

	it('rejects any backslash or control character, even when it would stay on this origin', () => {
		expect(safeNextPath('/notebooks\\123')).toBe('/');
		expect(safeNextPath('/notebooks/\u0000')).toBe('/');
		expect(safeNextPath('/notebooks/\u007f')).toBe('/');
		expect(safeNextPath('/notebooks/\u0085')).toBe('/');
	});
});
