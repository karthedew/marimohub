// Bearer tokens live in localStorage, which the server cannot see. Rendering
// on the server would always produce a signed-out shell and then thrash on
// hydration, so this app renders client-side only.
export const ssr = false;
