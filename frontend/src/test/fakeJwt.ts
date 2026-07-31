// A JWT-shaped-enough token for tests: only the payload's `sub` claim needs to
// decode correctly, since that's all the client ever reads out of a token.
export function fakeJwt(sub: string) {
	const header = btoa(JSON.stringify({ alg: 'none', typ: 'JWT' }));
	const payload = btoa(JSON.stringify({ sub }));
	return `${header}.${payload}.signature`;
}
