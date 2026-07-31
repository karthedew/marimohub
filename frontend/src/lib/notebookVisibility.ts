import type { NotebookVisibility } from '$lib/api';

export const VISIBILITY_LABELS: Record<NotebookVisibility, string> = {
	private: 'Private',
	unlisted: 'Unlisted',
	public: 'Public'
};

export const VISIBILITY_EXPLANATIONS: Record<NotebookVisibility, string> = {
	private: 'Only members of this Workspace can open it.',
	unlisted: 'Anyone with the direct link can open it; it stays out of Discover for non-members.',
	public: 'Anyone can open it, and it appears in Discover.'
};

export const VISIBILITY_ORDER: NotebookVisibility[] = ['private', 'unlisted', 'public'];
