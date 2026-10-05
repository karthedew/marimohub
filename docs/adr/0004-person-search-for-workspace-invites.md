# Person search for adding Workspace Members

Status: accepted

An Owner adds a Workspace Member by user id, and until now had to get that id from the person. Owners can now find people with `GET /api/workspaces/{id}/member-candidates?q=`, which resolves a person to a user id. Adding them is unchanged: `POST /api/workspaces/{id}/members` with that `user_id`. Registration is open and any User can create a Workspace and become its Owner, so whatever this search shows an Owner, it shows to anyone. We therefore built a narrow lookup, not a user directory, so that it cannot be used to harvest email addresses:

- Only Owners of an active Workspace can search. Everyone else gets the same 401, 403 or 404 as on the other owner-only member routes, before the query is even checked. A query must be 2 to 255 characters once trimmed, with no control character. A search returns at most 10 people, and never anyone who is already a member.
- A name search matches usernames and Display Names, never email addresses, and never finds anyone whose username or Display Name contains "@", since that may be their address. A query that contains "@" is an email lookup instead. It matches one whole address, ignoring case, so a prefix, a local part or a bare domain finds nobody. It only confirms an address the caller already knows, which registration's "already exists" conflict reveals anyway.
- Only that exact email match returns the full address. Every other result carries a masked hint: the first character of the address, "•••@", then the domain. That is enough to tell two people with the same name apart.
- Adding someone found this way reveals no more. Member lists, and the add and role-change responses, mask every member's email the same way, except the caller's own. Otherwise an Owner could add, read and remove person after person, since excluding members turns the 10-result cap into paging. A review scripted 27 full addresses in 58 requests that way.
- Nothing else spells the address out. A username a sign-in provider creates is never derived from the email, so it cannot be the address's local part. A provider's name claim that contains "@" is ignored. The production server drops query strings, and with them search text, from its access log.
- Usernames and Display Names cannot hide characters that change how they look or render: invisible ones, bidi overrides, line breaks. A new username also cannot use look-alike compatibility forms such as full-width letters.

We rejected three alternatives. A general `GET /api/users?q=` directory would list people nobody asked for. A prefix or substring email search would list everyone at a domain. Adding members directly by email or username would turn every membership change into an email lookup. The search is a separate surface, so add-member keeps its stable `user_id` contract.

Consequences:

- Usernames and Display Names are effectively public to anyone who creates a Workspace, and repeated queries can enumerate them. There is no rate limiting.
- Adding someone still needs no consent from them. Owners can no longer read their members' addresses in MarimoHub; invitations the person must accept would be the way to give that back. Masking was chosen provisionally, and the product owner still has to confirm it.
- The exact email lookup confirms guesses. With the domain in every hint, an address built from the person's name (`john.doe@` for "John Doe") can be confirmed in a guess or two.
- Accounts a provider created before this change keep their usernames, so some still equal their address's local part.
- An impostor can register another username with the same Display Name and an unverified address that masks to the same hint. The Owner tells them apart by username.
