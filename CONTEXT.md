# MarimoHub

MarimoHub lets users create, collaborate on, publish, run, and deploy marimo notebooks inside explicitly created workspaces.

## Language

**User**:
A person represented by a MarimoHub identity and able to authenticate to the application.
_Avoid_: Account when referring to the person

**Display Name**:
A User's own name, shown beside their username; optional, not unique, and never used to sign in.
_Avoid_: Real name

**Workspace**:
The ownership and collaboration boundary for notebooks.
_Avoid_: Personal workspace, default workspace

**Workspace Member**:
A User associated with a Workspace as an Owner, Editor, or Viewer.
_Avoid_: Collaborator when the role matters

**Owner**:
A Workspace Member who can administer the Workspace and perform every Editor operation.
_Avoid_: Notebook owner

**Member Candidate**:
A User who is not a Workspace Member of a given Workspace, as an Owner's person search for that Workspace reveals them.
_Avoid_: Invitee, directory entry

**Editor**:
A Workspace Member who can create, edit, fork, and deploy notebooks in the Workspace.

**Viewer**:
A Workspace Member who can read every notebook in the Workspace but cannot modify them.

**Notebook**:
A marimo document owned by exactly one Workspace, with optional creator attribution and lineage to a parent Notebook.
_Avoid_: Draft

**Notebook Visibility**:
The rule controlling who may read a Notebook and whether it appears in discovery.
_Avoid_: Publication status

**Private**:
A Notebook Visibility readable only by members of its Workspace.
_Avoid_: Draft

**Unlisted**:
A Notebook Visibility readable by direct link but excluded from discovery for non-members.

**Public**:
A Notebook Visibility readable by anyone and included in discovery.

**Deployment**:
A publicly accessible running application produced from a snapshot of a Notebook, independent of Notebook Visibility.
_Avoid_: Private deployment

**Session**:
An ephemeral edit or run environment for a Notebook.

**Runtime**:
An execution environment that serves either one Session or one Deployment.

**Workspace Files**:
The durable directory of files a Workspace owns, shared by every edit and run Session of its Notebooks.
_Avoid_: Home directory, user storage

**Archive**:
The reversible removal of a Workspace from normal use until its purge deadline.
_Avoid_: Delete workspace

**Purge**:
The permanent removal of an archived Workspace after its retention period.
_Avoid_: Archive

## Relationships

- A **User** can be a **Workspace Member** of many **Workspaces**
- A **User** may have one **Display Name**, which other Users may share
- A **Workspace** has one or more **Workspace Members** and must retain an **Owner**
- An **Owner** finds **Member Candidates** by name, username, exact email address, or user id; no one can browse every **User**
- A **Workspace** owns zero or more **Notebooks**
- A **Notebook** is owned by exactly one **Workspace**
- A **Notebook** may be attributed to one **User**, but attribution is not ownership
- A **Notebook** may be forked from one parent **Notebook**
- A **Notebook** may have one **Deployment**
- A **Notebook** may have many ephemeral **Sessions**
- A **Session** has one ephemeral **Runtime**
- A **Deployment** has one durable **Runtime**, which may sleep while the Deployment remains active
- A **Runtime** has an ephemeral filesystem; only Notebook source, explicitly stored Notebook data, and **Workspace Files** survive Runtime replacement
- A **Workspace** has one set of **Workspace Files**: edit **Sessions** can change them, run **Sessions** can only read them, and **Deployments** never see them
- A **Deployment** is public even when its source **Notebook** is Private or Unlisted
- An **Archived** Workspace may be restored before it is **Purged**
- Archiving a **Workspace** stops all of its **Sessions** and **Deployments**

## Example Dialogue

> **Dev:** "Does making this Notebook Private stop its Deployment?"
> **Domain expert:** "No. Notebook Visibility controls access to the Notebook and source; the Deployment is a separate public application."

## Flagged Ambiguities

- "owner" previously meant the User attached directly to a Notebook; resolved: a Workspace owns the Notebook, while Owner is a Workspace Member role.
- "draft" previously meant a private Notebook; resolved: Private is a visibility rule, not a lifecycle state.
- "publish" was used for every visibility change; resolved: the UI changes Notebook Visibility, including back to Private.
- "delete workspace" meant a reversible operation; resolved: users Archive a Workspace, while Purge is permanent.
- "session" was used for both an edit/run Session and a Deployment's execution environment; resolved: Runtime is the shared execution concept, while Session remains specific to edit/run.
- "deployment source" could mean the current Notebook or the source captured when deployed; resolved: a Deployment runs an immutable deploy-time snapshot until explicitly redeployed.
- "invite" was used for adding a Workspace Member; resolved: an Owner adds a Member Candidate directly, and there is no invitation for the person to accept.
