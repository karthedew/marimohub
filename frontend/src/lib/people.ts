// How a person is named on screen. A display name is optional and not unique,
// so wherever one shows, the (unique) username shows with it.
export type PersonName = {
	username: string;
	display_name?: string | null;
};

export function displayNameOf(person: PersonName): string | null {
	const name = person.display_name?.trim();
	return name ? name : null;
}

// One line naming the person unambiguously, e.g. for a filled-in input.
export function personLabel(person: PersonName): string {
	const name = displayNameOf(person);
	return name ? `${name} (${person.username})` : person.username;
}

// Avatar initials: first and last word of a display name, otherwise the start
// of the username. Split by code point so an emoji is never cut in half.
export function personInitials(person: PersonName): string {
	const name = displayNameOf(person);
	if (name) {
		const words = name.split(/\s+/);
		const letters =
			words.length > 1 ? firstCharacter(words[0]) + firstCharacter(words[words.length - 1]) : leading(name, 2);
		return letters.toUpperCase();
	}
	return leading(person.username, 2).toUpperCase();
}

function leading(value: string, count: number) {
	return Array.from(value).slice(0, count).join('');
}

function firstCharacter(value: string) {
	return leading(value, 1);
}
