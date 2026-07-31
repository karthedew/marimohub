# MarimoHub

MarimoHub lets users create, collaborate on, publish, run, and deploy marimo notebooks inside explicitly created workspaces.

## Language

**User**:
A person represented by a MarimoHub identity and able to authenticate to the application.
_Avoid_: Account when referring to the person

**Workspace**:
The ownership and collaboration boundary for notebooks.
_Avoid_: Personal workspace, default workspace

**Workspace Member**:
A User associated with a Workspace as an Owner, Editor, or Viewer.
_Avoid_: Collaborator when the role matters

**Owner**:
A Workspace Member who can administer the Workspace and perform every Editor operation.
_Avoid_: Notebook owner

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
A publicly accessible running application produced from a Notebook, independent of Notebook Visibility.
_Avoid_: Private deployment

**Session**:
An ephemeral edit or run environment for a Notebook.

**Archive**:
The reversible removal of a Workspace from normal use until its purge deadline.
_Avoid_: Delete workspace

**Purge**:
The permanent removal of an archived Workspace after its retention period.
_Avoid_: Archive

## Relationships

- A **User** can be a **Workspace Member** of many **Workspaces**
- A **Workspace** has one or more **Workspace Members** and must retain an **Owner**
- A **Workspace** owns zero or more **Notebooks**
- A **Notebook** is owned by exactly one **Workspace**
- A **Notebook** may be attributed to one **User**, but attribution is not ownership
- A **Notebook** may be forked from one parent **Notebook**
- A **Notebook** may have one **Deployment**
- A **Notebook** may have many ephemeral **Sessions**
- A **Deployment** is public even when its source **Notebook** is Private or Unlisted
- An **Archived** Workspace may be restored before it is **Purged**

## Example Dialogue

> **Dev:** "Does making this Notebook Private stop its Deployment?"
> **Domain expert:** "No. Notebook Visibility controls access to the Notebook and source; the Deployment is a separate public application."

## Flagged Ambiguities

- "owner" previously meant the User attached directly to a Notebook; resolved: a Workspace owns the Notebook, while Owner is a Workspace Member role.
- "draft" previously meant a private Notebook; resolved: Private is a visibility rule, not a lifecycle state.
- "publish" was used for every visibility change; resolved: the UI changes Notebook Visibility, including back to Private.
- "delete workspace" meant a reversible operation; resolved: users Archive a Workspace, while Purge is permanent.
