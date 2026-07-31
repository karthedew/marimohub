import { describe, expect, it } from 'vitest';

import { decideWakeStep, nextWakeDelay } from './deploymentWake';

const timing = (overrides: Partial<{ elapsedMs: number; timeoutMs: number; nextDelayMs: number }> = {}) => ({
	elapsedMs: 0,
	timeoutMs: 20_000,
	nextDelayMs: 1000,
	...overrides
});

describe('decideWakeStep', () => {
	it('reports ready on success regardless of elapsed time', () => {
		expect(decideWakeStep({ ok: true }, timing({ elapsedMs: 19_999 }))).toEqual({ action: 'ready' });
	});

	it('stops immediately on a 404 and surfaces the backend detail', () => {
		const result = decideWakeStep({ ok: false, status: 404, detail: 'Deployment not found' }, timing());
		expect(result).toEqual({ action: 'stop', message: 'Deployment not found' });
	});

	it('stops immediately on any other terminal status', () => {
		const result = decideWakeStep({ ok: false, status: 500, detail: 'Internal error' }, timing());
		expect(result).toEqual({ action: 'stop', message: 'Internal error' });
	});

	it('retries a 503 within the timeout window', () => {
		const result = decideWakeStep({ ok: false, status: 503, detail: 'Service unavailable' }, timing({ elapsedMs: 5000, nextDelayMs: 1500 }));
		expect(result).toEqual({ action: 'retry', delayMs: 1500 });
	});

	it('retries a 504 within the timeout window', () => {
		const result = decideWakeStep({ ok: false, status: 504, detail: 'Gateway timeout' }, timing({ elapsedMs: 5000 }));
		expect(result.action).toBe('retry');
	});

	it('stops a 503 once the elapsed time reaches the bound instead of retrying forever', () => {
		const result = decideWakeStep(
			{ ok: false, status: 503, detail: 'Service unavailable' },
			timing({ elapsedMs: 20_000, timeoutMs: 20_000 })
		);
		expect(result).toEqual({ action: 'stop', message: 'Deployment did not wake up in time.' });
	});

	it('never retries a 429 — it reports capacity exhaustion for a manual retry instead', () => {
		const result = decideWakeStep({ ok: false, status: 429, detail: 'No capacity available' }, timing({ elapsedMs: 0 }));
		expect(result).toEqual({ action: 'capacity', message: 'No capacity available' });
	});

	it('reports capacity exhaustion even right at the timeout bound, never falling through to a generic stop', () => {
		const result = decideWakeStep({ ok: false, status: 429, detail: 'No capacity available' }, timing({ elapsedMs: 20_000 }));
		expect(result.action).toBe('capacity');
	});
});

describe('nextWakeDelay', () => {
	it('grows the delay but caps it', () => {
		expect(nextWakeDelay(1000)).toBe(1500);
		expect(nextWakeDelay(2800)).toBe(3000);
		expect(nextWakeDelay(3000)).toBe(3000);
	});
});
