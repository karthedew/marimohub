import { expect, test } from '@playwright/test';

import { addMember, createWorkspace, openWorkspace, register, registerByApi, unique, userId } from './helpers';

test('registering with no return path lands on the Notebook home', async ({ page }) => {
	await register(page, unique('reguser'));

	await expect(page).toHaveURL(/\/$/);
	await expect(page.getByRole('heading', { name: 'Notebooks' })).toBeVisible();
	await expect(page.getByRole('heading', { name: 'Create your first Workspace' })).toBeVisible();
});

test('creates a workspace and surfaces a slug conflict inline instead of navigating away', async ({ page }) => {
	await register(page, unique('creator'));

	const slug = unique('team').toLowerCase();
	await createWorkspace(page, 'Team One', slug);
	await expect(page.getByRole('link', { name: /Team One/ })).toBeVisible();

	await createWorkspace(page, 'Team Two', slug);
	await expect(page).toHaveURL(/\/workspaces$/);
	await expect(page.getByRole('alert')).toContainText(/already in use/i);
	await expect(page.getByRole('link', { name: /Team Two/ })).toHaveCount(0);
});

test('copies the signed-in User ID to the clipboard', async ({ page, context, baseURL }) => {
	await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: baseURL });
	await register(page, unique('copyuser'));

	const id = await userId(page);
	expect(id.length).toBeGreaterThan(0);

	await page.goto('/settings');
	await page.getByRole('button', { name: 'Copy' }).click();
	await expect(page.getByRole('button', { name: 'Copied' })).toBeVisible();

	const clipboardText = await page.evaluate(() => navigator.clipboard.readText());
	expect(clipboardText).toBe(id);
});

test('an owner can add, promote, demote, and remove a member; the removed member loses access', async ({ browser }) => {
	const ownerContext = await browser.newContext();
	const memberContext = await browser.newContext();
	const ownerPage = await ownerContext.newPage();
	const memberPage = await memberContext.newPage();

	try {
		const memberName = unique('member');
		await register(ownerPage, unique('owner'));
		await register(memberPage, memberName, { displayName: 'Mina Member' });
		const memberId = await userId(memberPage);

		await createWorkspace(ownerPage, 'Collab Space');
		await openWorkspace(ownerPage, 'Collab Space');

		const addButton = ownerPage.getByRole('button', { name: 'Add member' });
		await expect(addButton).toBeDisabled();

		const person = ownerPage.getByRole('combobox', { name: 'Person' });
		await person.fill(memberName);
		const option = ownerPage.getByRole('option', { name: new RegExp(`@${memberName},`) });
		await expect(option).toContainText('Mina Member');
		// Found by username, the address stays masked: only its first letter and domain show.
		await expect(option).toContainText(`${memberName[0]}•••@example.com`);
		await expect(option).not.toContainText(`${memberName}@example.com`);
		await option.click();
		await expect(person).toHaveValue(`Mina Member (${memberName})`);
		await expect(addButton).toBeEnabled();

		await ownerPage.getByLabel('Role').selectOption('viewer');
		await addButton.click();
		const memberRow = ownerPage.locator('tr', { hasText: memberId });
		await expect(memberRow).toBeVisible();
		await expect(memberRow).toContainText('Mina Member');
		await expect(memberRow).toContainText(`@${memberName}`);
		await expect(memberRow.getByRole('combobox')).toHaveValue('viewer');
		// The form starts over for the next person.
		await expect(person).toHaveValue('');
		await expect(ownerPage.getByLabel('Role')).toHaveValue('editor');
		await expect(addButton).toBeDisabled();

		await memberPage.goto('/workspaces');
		await expect(memberPage.getByRole('link', { name: /Collab Space/ })).toBeVisible();

		await memberRow.getByRole('combobox').selectOption('editor');
		await expect(memberRow.getByRole('combobox')).toHaveValue('editor');

		await memberRow.getByRole('combobox').selectOption('viewer');
		await expect(memberRow.getByRole('combobox')).toHaveValue('viewer');

		await memberRow.getByRole('button', { name: 'Remove' }).click();
		await expect(ownerPage.locator('tr', { hasText: memberId })).toHaveCount(0);

		await memberPage.reload();
		await expect(memberPage.getByRole('link', { name: /Collab Space/ })).toHaveCount(0);
	} finally {
		await ownerContext.close();
		await memberContext.close();
	}
});

test('an owner finds someone by their exact email address, never by part of one', async ({ browser }) => {
	const ownerContext = await browser.newContext();
	const personContext = await browser.newContext();
	const ownerPage = await ownerContext.newPage();
	const personPage = await personContext.newPage();

	try {
		const personName = unique('emailee');
		const email = `${personName}@example.com`;
		await register(ownerPage, unique('emailowner'));
		await register(personPage, personName);
		const personId = await userId(personPage);

		await createWorkspace(ownerPage, 'Email Space');
		await openWorkspace(ownerPage, 'Email Space');

		const person = ownerPage.getByRole('combobox', { name: 'Person' });
		// Scoped to the listbox: the Role selects' <option>s are options too.
		const options = ownerPage.getByRole('listbox', { name: 'Matching people' }).getByRole('option');
		// Nothing short of the whole address matches, so nobody can list everyone
		// at a domain or work their way along an address.
		for (const partial of ['@example.com', `${personName}@example`, email.slice(1)]) {
			await person.fill(partial);
			await expect(ownerPage.getByText('No matching people')).toBeVisible();
			await expect(options).toHaveCount(0);
		}

		// The exact address finds them, stray spaces and capitals aside, and shows
		// it in full: the Owner typed it, so showing it reveals nothing new.
		await person.fill(`  ${email.toUpperCase()}  `);
		const option = ownerPage.getByRole('option', { name: new RegExp(`@${personName},`) });
		await expect(option).toContainText(email, { ignoreCase: true });

		// Keyboard only: ArrowDown points at the person, Enter chooses them.
		await person.press('ArrowDown');
		await expect(option).toHaveAttribute('aria-selected', 'true');
		const optionId = await option.getAttribute('id');
		expect(optionId).toBeTruthy();
		await expect(person).toHaveAttribute('aria-activedescendant', optionId!);
		await person.press('Enter');
		await expect(person).toHaveValue(personName);

		await ownerPage.getByLabel('Role').selectOption('editor');
		await ownerPage.getByRole('button', { name: 'Add member' }).click();
		const row = ownerPage.locator('tr', { hasText: personId });
		await expect(row).toBeVisible();
		// The members list masks everyone's address but the caller's own, so
		// adding someone never reveals more than the search did.
		await expect(row).toContainText(`${personName[0]}•••@example.com`);
		await expect(row).not.toContainText(email);
		await expect(row.getByRole('combobox')).toHaveValue('editor');

		// A member is never offered again.
		await person.fill(email);
		await expect(ownerPage.getByText('No matching people')).toBeVisible();
	} finally {
		await ownerContext.close();
		await personContext.close();
	}
});

test('a full list says it may be cut short, stays open as the Owner types on, and outlines the active option', async ({
	page,
	request
}) => {
	// Eleven people whose usernames share a prefix: one more than a search returns.
	const prefix = unique('crowd');
	for (let index = 1; index <= 11; index += 1) {
		await registerByApi(request, `${prefix}${String(index).padStart(2, '0')}`);
	}
	await register(page, unique('crowdowner'));
	await createWorkspace(page, 'Crowd Space');
	await openWorkspace(page, 'Crowd Space');

	const person = page.getByRole('combobox', { name: 'Person' });
	const options = page.getByRole('listbox', { name: 'Matching people' }).getByRole('option');
	const cutShort = page.getByText('Showing the first 10 matches. Keep typing to narrow the search.');
	await person.fill(prefix);
	await expect(options).toHaveCount(10);
	await expect(cutShort).toBeVisible();

	// The options never take focus, so the one the keyboard is on draws the
	// focus ring itself; the others do not.
	await person.press('ArrowDown');
	await person.press('ArrowDown');
	await expect(options.nth(1)).toHaveAttribute('aria-selected', 'true');
	await expect(options.nth(1)).toHaveCSS('outline-style', 'solid');
	await expect(options.nth(1)).toHaveCSS('outline-width', '2px');
	await expect(options.nth(0)).toHaveCSS('outline-style', 'none');

	// Typing on keeps the people listed, and the listbox open, until the next
	// answer lands, held back here until the checks are done.
	let release!: () => void;
	const held = new Promise<void>((resolve) => (release = resolve));
	await page.route(
		(url) => url.pathname.endsWith('/member-candidates'),
		async (route) => {
			if (route.request().method() === 'GET') await held;
			await route.continue();
		}
	);
	await person.press('1');
	await expect(person).toHaveAttribute('aria-expanded', 'true');
	await expect(options).toHaveCount(10);
	// Typing moves the keyboard back to the text: no option is active.
	await expect(person).not.toHaveAttribute('aria-activedescendant');

	release();
	await expect(options).toHaveCount(2);
	await expect(options.first()).toHaveAccessibleName(new RegExp(`@${prefix}10,`));
	await expect(cutShort).toBeHidden();
});

test('a failed add hands focus back to the Person field, the person still chosen', async ({ page, request }) => {
	const someone = unique('refused');
	await registerByApi(request, someone);
	await register(page, unique('refuser'));
	await createWorkspace(page, 'Refusal Space');
	await openWorkspace(page, 'Refusal Space');

	// Every add is refused, as when another Owner added the person a moment earlier.
	await page.route(
		(url) => url.pathname.endsWith('/members'),
		async (route) => {
			if (route.request().method() !== 'POST') return route.continue();
			await route.fulfill({
				status: 409,
				contentType: 'application/json',
				body: JSON.stringify({ detail: 'User is already a member of this workspace' })
			});
		}
	);

	const person = page.getByRole('combobox', { name: 'Person' });
	const addButton = page.getByRole('button', { name: 'Add member' });
	await person.fill(someone);
	await page.getByRole('listbox', { name: 'Matching people' }).getByRole('option').first().click();

	// Sent with Enter from the field, or with the button: either way focus comes
	// back to the field instead of falling to the page while the form is disabled.
	for (const send of [() => person.press('Enter'), () => addButton.click()]) {
		const refused = page.waitForResponse(
			(response) => response.url().endsWith('/members') && response.request().method() === 'POST'
		);
		await send();
		await refused;
		await expect(page.getByRole('alert')).toContainText('already a member');
		await expect(person).toBeFocused();
		await expect(person).toHaveValue(someone);
		await expect(addButton).toBeEnabled();
	}
});

test('a long display name is cut short in the members table, leaving Role and Remove in view', async ({
	page,
	request
}) => {
	const username = unique('longname');
	// 105 characters; the backend allows 255.
	const displayName = `Maximiliana Theodora ${'Wolfeschlegelsteinhausenbergerdorff '.repeat(2)}Cholmondeley`;
	await registerByApi(request, username, { displayName });
	await register(page, unique('longowner'));
	await createWorkspace(page, 'Long Name Space');
	await openWorkspace(page, 'Long Name Space');
	await addMember(page, username, 'viewer');

	const row = page.locator('tr', { hasText: `@${username}` });
	await expect(row).toBeVisible();
	// The whole name is a hover away ...
	await expect(row.getByTitle(`${displayName} @${username}`, { exact: true })).toBeVisible();
	// ... and the table still fits its card rather than scrolling sideways.
	const overflow = await page
		.getByRole('table', { name: 'Workspace members' })
		.evaluate((table) => table.parentElement!.scrollWidth - table.parentElement!.clientWidth);
	expect(overflow).toBeLessThanOrEqual(0);
	await expect(row.getByRole('combobox')).toBeInViewport();
	await expect(row.getByRole('button', { name: 'Remove' })).toBeInViewport();
});

test('the backend refuses to demote or remove the sole owner of a workspace', async ({ page }) => {
	await register(page, unique('soleowner'));
	const id = await userId(page);

	await createWorkspace(page, 'Solo Space');
	await openWorkspace(page, 'Solo Space');

	const ownRow = page.locator('tr', { hasText: id });
	await ownRow.getByRole('combobox').selectOption('editor');
	await expect(page.getByRole('alert')).toContainText(/at least one owner/i);

	await ownRow.getByRole('button', { name: 'Remove' }).click();
	await expect(page.getByRole('alert')).toContainText(/at least one owner/i);
});

test('archiving a workspace returns to Workspaces, and restore brings it back', async ({ page }) => {
	await register(page, unique('archiver'));

	await createWorkspace(page, 'Scratch Space');
	await openWorkspace(page, 'Scratch Space');

	await page.getByRole('button', { name: 'Archive workspace' }).click();
	await page.getByRole('button', { name: 'Yes, archive this workspace' }).click();

	await page.waitForURL(/\/workspaces\?notice=workspace-archived$/);
	await expect(page.getByRole('status')).toContainText(/archived/i);
	await expect(page.getByRole('link', { name: /Scratch Space/ })).toHaveCount(0);

	// Scoped to the page body: the sidebar also links to Archived workspaces, and
	// the header's workspace switcher names the active Workspace once restored.
	await page.getByRole('main').getByRole('link', { name: 'Archived workspaces' }).click();
	await expect(page.getByRole('main').getByText('Scratch Space')).toBeVisible();

	await page.getByRole('button', { name: 'Restore' }).click();
	await expect(page.getByRole('main').getByText('Scratch Space')).toHaveCount(0);

	await page.goto('/workspaces');
	await expect(page.getByRole('link', { name: /Scratch Space/ })).toBeVisible();
});

test('only the Owner sees Rename; an Editor sees the name as read-only', async ({ browser }) => {
	const ownerContext = await browser.newContext();
	const editorContext = await browser.newContext();
	const ownerPage = await ownerContext.newPage();
	const editorPage = await editorContext.newPage();

	try {
		await register(ownerPage, unique('renameowner'));
		await register(editorPage, unique('renameeditor'));
		const editorId = await userId(editorPage);

		await createWorkspace(ownerPage, 'Rename Space');
		await openWorkspace(ownerPage, 'Rename Space');
		// A pasted User ID finds the person as well as a name does.
		await addMember(ownerPage, editorId, 'editor');
		await expect(ownerPage.locator('tr', { hasText: editorId })).toBeVisible();

		await expect(ownerPage.getByRole('button', { name: 'Rename' })).toBeVisible();
		await ownerPage.getByRole('button', { name: 'Rename' }).click();
		await ownerPage.getByLabel('Name', { exact: true }).fill('Renamed Space');
		await ownerPage.getByRole('button', { name: 'Save' }).click();
		await expect(ownerPage.getByRole('heading', { name: 'Renamed Space' })).toBeVisible();

		await editorPage.goto('/workspaces');
		await openWorkspace(editorPage, 'Renamed Space');
		await expect(editorPage.getByRole('heading', { name: 'Renamed Space' })).toBeVisible();
		await expect(editorPage.getByRole('button', { name: 'Rename' })).toHaveCount(0);
		// An Editor administers Notebooks, not the Workspace itself: member
		// management and archive stay Owner-only regardless of write capability.
		await expect(editorPage.getByRole('combobox', { name: 'Person' })).toHaveCount(0);
		await expect(editorPage.getByRole('button', { name: 'Archive workspace' })).toHaveCount(0);
	} finally {
		await ownerContext.close();
		await editorContext.close();
	}
});

test('every workspace route survives a hard refresh while authenticated', async ({ page }) => {
	await register(page, unique('refresher'));

	await createWorkspace(page, 'Refresh Space');
	await openWorkspace(page, 'Refresh Space');
	const detailUrl = page.url();

	await page.reload();
	await expect(page.getByRole('heading', { name: 'Refresh Space' })).toBeVisible();

	await page.goto('/workspaces');
	await page.reload();
	await expect(page.getByRole('heading', { name: 'Workspaces' })).toBeVisible();

	await page.goto('/workspaces/archived');
	await page.reload();
	await expect(page.getByRole('heading', { name: 'Archived workspaces' })).toBeVisible();

	await page.goto(detailUrl);
	await page.reload();
	await expect(page.getByRole('heading', { name: 'Refresh Space' })).toBeVisible();
});
