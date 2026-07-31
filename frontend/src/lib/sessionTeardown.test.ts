import { beforeEach, describe, expect, it, vi } from 'vitest';

const deleteMock = vi.fn().mockResolvedValue(undefined);

vi.mock('$lib/api', () => ({
	api: { sessions: { delete: deleteMock } }
}));

const { teardownSessionOnUnload } = await import('./sessionTeardown');

beforeEach(() => {
	deleteMock.mockClear();
});

describe('teardownSessionOnUnload', () => {
	it('sends an unauthenticated, keepalive DELETE for the session', () => {
		teardownSessionOnUnload('session-1');

		expect(deleteMock).toHaveBeenCalledExactlyOnceWith('session-1', { auth: false, keepalive: true });
	});

	it('does not return a promise the caller could accidentally await or chain', () => {
		const result = teardownSessionOnUnload('session-1');
		expect(result).toBeUndefined();
	});
});
