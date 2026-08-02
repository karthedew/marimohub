import { describe, expect, it } from 'vitest';

import { GET } from './+server';

describe('GET /healthz', () => {
	it('returns a 200 ok payload', async () => {
		const response = GET();

		expect(response.status).toBe(200);
		expect(await response.json()).toEqual({ status: 'ok' });
	});
});
