// Pure decision logic for waking a sleeping public deployment, kept separate
// from the polling/timer orchestration in the deploy page so the 429-vs-503
// distinction can be unit tested without fighting real timers.

export type WakeAttemptResult = { ok: true } | { ok: false; status: number; detail: string };

export type WakeDecision =
	| { action: 'ready' }
	// 503/504 mean the upstream isn't serving yet; retry after `delayMs`.
	| { action: 'retry'; delayMs: number }
	// 429 means the backend has no room for another session right now — more
	// retries would just add to the queue, so this needs a human decision.
	| { action: 'capacity'; message: string }
	// A 404 or any other terminal response; no further attempts are useful.
	| { action: 'stop'; message: string };

const RETRYABLE_STATUSES = new Set([503, 504]);

export function decideWakeStep(
	result: WakeAttemptResult,
	timing: { elapsedMs: number; timeoutMs: number; nextDelayMs: number }
): WakeDecision {
	if (result.ok) return { action: 'ready' };
	if (result.status === 429) return { action: 'capacity', message: result.detail };
	if (!RETRYABLE_STATUSES.has(result.status)) return { action: 'stop', message: result.detail };
	if (timing.elapsedMs >= timing.timeoutMs) {
		return { action: 'stop', message: 'Deployment did not wake up in time.' };
	}
	return { action: 'retry', delayMs: timing.nextDelayMs };
}

export function nextWakeDelay(current: number, capMs = 3000): number {
	return Math.min(current * 1.5, capMs);
}
