// Shared Tailwind class strings for the form and surface patterns every page repeats.
// Kept as plain strings (not components) so a field keeps its native element, id,
// label association, and bindings exactly where the page declares them.

export const card = 'rounded-lg border border-app-line bg-app-card';

export const cardHeader = 'border-b border-app-line px-5 py-4';

export const cardTitle = 'm-0 border-0 p-0 text-base font-semibold text-app-fg';

export const sectionLabel = 'text-[10px] font-semibold uppercase tracking-[0.11em] text-app-muted';

export const fieldLabel = 'text-xs font-semibold text-app-muted';

export const input =
	'h-10 w-full rounded-md border border-app-line bg-app-bg px-3 text-sm text-app-fg outline-none transition placeholder:text-app-muted focus:border-brand-strong disabled:cursor-not-allowed disabled:opacity-60 aria-[invalid=true]:border-app-danger';

export const select =
	'h-10 w-full rounded-md border border-app-line bg-app-bg px-2.5 text-sm text-app-fg outline-none transition focus:border-brand-strong disabled:cursor-not-allowed disabled:opacity-60';

export const textarea =
	'min-h-28 w-full rounded-md border border-app-line bg-app-bg p-3 text-sm text-app-fg outline-none transition placeholder:text-app-muted focus:border-brand-strong disabled:cursor-not-allowed disabled:opacity-60';

export const fieldHint = 'text-xs text-app-muted';

export const fieldError = 'text-xs font-medium text-app-danger';

export const errorBanner =
	'rounded-md border border-app-danger/35 bg-app-danger/10 px-3.5 py-3 text-sm text-app-danger';

export const noticeBanner =
	'rounded-md border border-app-note-line bg-app-note-bg px-3.5 py-3 text-sm text-app-fg';

export const tagChip =
	'inline-flex items-center rounded-md border border-app-line bg-app-bg px-1.5 py-0.5 text-[11px] font-medium text-app-muted';

export const iconTile =
	'grid shrink-0 place-items-center rounded-md border border-app-line bg-app-bg text-brand-strong';
