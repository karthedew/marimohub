# Runtime credentials and internal API isolation

Status: accepted

MarimoHub Runtimes use a high-entropy opaque credential whose lifetime is the owning `MarimoSession`, rather than an expiring backend-signed JWT or a Kubernetes ServiceAccount identity. The backend authors the immutable credential Secret, the internal API validates the presented credential against the live Secret and CR before checking the Notebook binding, and deletion of the CR revokes the credential. Runtime Pods do not receive Kubernetes credentials.

The internal API runs as a separate, unrouted workload reachable only from the Runtime namespace over TLS. This adds Kubernetes reads to credential verification, but it avoids an unsolved refresh protocol for indefinitely active or infrastructure-recreated Runtimes, keeps workload credentials off the public listener, and makes revocation follow Runtime lifecycle. A short cache may be added later only if it preserves bounded revocation and fail-closed behavior.
