# Administrator shared volumes

Status: accepted

Administrators can mount data volumes, typically NFS exports such as team datasets, into every notebook. They list them in Helm values under `runtime.sharedVolumes`. There is no in-app admin role or screen, the same as every other piece of Runtime policy in this chart. The chart passes the list to the operator as `--shared-volumes`, and the operator applies the defaults and validates the list in one place before it starts.

Each share names a ReadWriteMany PersistentVolumeClaim in the Runtime namespace, never an inline `nfs:` volume. The restricted Pod Security profile and OpenShift's `restricted-v2` both forbid inline NFS, so an NFS export reaches notebooks through an `nfs:` PersistentVolume bound to that claim. As with Workspace Files (ADR 0002), the platform creates the claim and the chart only references it.

Shares are read-only by default and mount at `/mnt/<name>` (or any `mountPath`, such as the export's own host path) into edit and run Runtimes, and only into the marimo container, never the init containers. A share can also list `deploy`, but only while it stays read-only, because Deployments are public applications. Unlike Workspace Files, every Workspace sees the same files, so a writable share is shared scratch space across Workspaces. Admins should make one writable only on purpose.

Adding, removing or changing a share changes the Runtime Pod template. When the operator restarts, it replaces running Pods whose template hash no longer matches, which ends their kernels.

The local kind environment mounts its whole NFS stand-in, `/data1/nfs`, at that same path in every notebook and Deployment, read-only. Notebook code then uses the same paths as the machine. Workspace Files live in a separate host directory, `/data1/marimohub`, so the shared mount never exposes them.
