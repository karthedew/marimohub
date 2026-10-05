# Workspace files on shared storage

Status: accepted

This supersedes the plan's earlier non-goal of "no durable Runtime filesystem or Workspace PVC". Each Workspace now owns one durable directory, `workspaces/<workspaceId>`, on a single administrator-provisioned ReadWriteMany claim in the Runtime namespace. In production that claim is an NFS export; locally it is a host directory, `/data1/marimohub` by default, kept apart from the shared NFS stand-in and bound as a hostPath PersistentVolume by `make kind-up`. The chart only names the claim (`runtime.workspaceStorage.existingClaim`) and never creates it, the same way it treats the pre-created namespaces and Secrets.

Access follows who may start each Runtime mode. Edit Runtimes, which only Editors and Owners can start, mount the directory read-write at `/work/workspace`. Run Runtimes, which anyone who can read the Notebook can start (including an anonymous visitor to a Public Notebook), mount it read-only. Deploy Runtimes do not mount it at all, because a Deployment serves an immutable deploy-time snapshot as a public application, and live, mutable files would break that guarantee. Each Runtime sees only its own Workspace's directory. A `workspace-init` container, running platform code before any notebook code, creates the directory with mode `2770`. We did not let the kubelet auto-create the subPath, because it would create it as root with the claim root's mode, which an arbitrary-UID Runtime could not write to.

The share's `workspaces/` directory must be group-writable and setgid. Runtime Pods join that group through `runtime.workspaceStorage.supplementalGroups`, because NFS honors neither `fsGroup` nor OpenShift's arbitrary UIDs. Runtimes keep their ephemeral scratch volumes, and everything outside `/work/workspace` is still lost when a Runtime is replaced.

Open follow-ups:

- Purge does not yet delete a Workspace's directory.
- There is no per-Workspace size limit beyond what the export itself enforces.
- Whether Deployments should ever see Workspace files, for example through a snapshot taken at deploy time, is undecided.
