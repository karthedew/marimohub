import { browser } from '$app/environment';
import { writable } from 'svelte/store';

const STORAGE_KEY = 'marimohub-profile-avatars';

function readStoredAvatars(): Record<string, string> {
	if (!browser) return {};
	try {
		const value = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}') as unknown;
		return value && typeof value === 'object' ? (value as Record<string, string>) : {};
	} catch {
		return {};
	}
}

function createProfileAvatarsStore() {
	const store = writable<Record<string, string>>(readStoredAvatars());

	function update(userId: string, avatar: string | null) {
		store.update((avatars) => {
			const next = { ...avatars };
			if (avatar) next[userId] = avatar;
			else delete next[userId];
			if (browser) {
				try {
					localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
				} catch {
					// A large image or restricted storage should not break profile settings.
				}
			}
			return next;
		});
	}

	return { subscribe: store.subscribe, update };
}

export const profileAvatars = createProfileAvatarsStore();
