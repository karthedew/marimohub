// Login and Register both accept a `?next=` query param naming where to land
// the user afterward. Because that value round-trips through a URL a caller
// controls, it must be constrained to a same-origin, absolute path before
// it's ever handed to `goto()` — otherwise it's an open-redirect primitive.
export function safeNextPath(value: string | null | undefined, fallback = '/') {
	if (!value || !value.startsWith('/') || value.startsWith('//')) return fallback;
	return value;
}
