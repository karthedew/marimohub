// Login, Register, and the provider sign-in callback all accept a `next` value
// naming where to land the user afterward. That value round-trips through a
// URL (or sessionStorage) a caller controls, so it must be constrained to a
// same-origin path before anything navigates to it — otherwise it is an
// open-redirect primitive.
//
// Starting with a single `/` is not enough on its own. The URL parser treats
// `\` like `/` and silently drops tabs and newlines, so `/\evil.example`,
// `/\t/evil.example`, and `/\n/evil.example` all resolve to
// `http://evil.example/`. Those characters are rejected outright, and the
// value must still resolve to the origin it was resolved against.
const RESOLUTION_BASE = 'http://next-path.invalid';

// C0 controls, DEL, C1 controls, and the backslash.
const FORBIDDEN_CHARACTERS = /[\u0000-\u001f\u007f-\u009f\\]/;

export function safeNextPath(value: string | null | undefined, fallback = '/') {
	if (!value || !value.startsWith('/') || value.startsWith('//')) return fallback;
	if (FORBIDDEN_CHARACTERS.test(value)) return fallback;

	try {
		if (new URL(value, RESOLUTION_BASE).origin !== RESOLUTION_BASE) return fallback;
	} catch {
		return fallback;
	}
	return value;
}
