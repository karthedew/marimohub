"""Cross-language Runtime naming and status contract.

Mirrors `marimohub-operator/internal/runtimecontract/names.go` (labels,
annotations, Secret naming) and the Condition type/Reason constants declared
in `marimohub-operator/api/v1alpha1/marimosession_types.go`. A conformance
test (`tests/test_runtime_contract.py`) reads both the Go source and this
module and fails on drift; there is deliberately no third, independently
maintained constants table either side tests against.
"""

GROUP = "marimohub.io"
VERSION = "v1alpha1"
PLURAL = "marimosessions"
API_VERSION = f"{GROUP}/{VERSION}"
KIND = "MarimoSession"

LABEL_NOTEBOOK = f"{GROUP}/notebook"
LABEL_WORKSPACE = f"{GROUP}/workspace"
LABEL_MODE = f"{GROUP}/mode"
LABEL_SESSION = f"{GROUP}/session"

# Both annotations carry a unique opaque value, never a timestamp: the
# operator timestamps activity itself using its own clock and only compares
# these values for change-detection.
ANNOTATION_WAKE_REQUEST = f"{GROUP}/wake-request"
ANNOTATION_ACTIVITY = f"{GROUP}/activity"

SECRET_KEY_MARIMO_TOKEN = "MARIMO_TOKEN"  # noqa: S105 -- a Secret key name, not a credential value
SECRET_KEY_RUNTIME_CREDENTIAL = "RUNTIME_CREDENTIAL"  # noqa: S105 -- ditto

# Must equal Go's `corev1.SecretTypeOpaque`, a Kubernetes API constant rather
# than a Go source literal, so there is nothing for the conformance test to
# compare this against textually; both sides just have to keep meaning
# "Opaque" (the type the operator's `validateOwnedSecret` requires).
SECRET_TYPE = "Opaque"  # noqa: S105 -- a Secret type name, not a credential value

RUNTIME_PORT = 8080

CONDITION_CREDENTIALS_AVAILABLE = "CredentialsAvailable"
CONDITION_CAPACITY_AVAILABLE = "CapacityAvailable"
CONDITION_READY = "Ready"
CONDITION_RECONCILED = "Reconciled"

REASON_CREDENTIALS_MISSING = "CredentialsMissing"
REASON_CREDENTIALS_INVALID = "CredentialsInvalid"
REASON_QUOTA_EXCEEDED = "QuotaExceeded"
REASON_RESOURCE_CONFLICT = "ResourceConflict"
REASON_INVALID_SPEC = "InvalidSpec"
REASON_IMAGE_UNAVAILABLE = "ImageUnavailable"
REASON_SOURCE_UNAVAILABLE = "SourceUnavailable"
REASON_RUNTIME_EXITED = "RuntimeExited"
REASON_OOM_KILLED = "OOMKilled"
REASON_INFRASTRUCTURE_LOST = "InfrastructureLost"
REASON_RECONCILE_SUCCEEDED = "ReconcileSucceeded"
REASON_PLATFORM_DENIED = "PlatformDenied"
REASON_STARTING = "Starting"
REASON_UNHEALTHY = "Unhealthy"


def secret_name(runtime_id: str) -> str:
    """Return the name of a Runtime's owned credential Secret."""
    return f"msess-{runtime_id}-env"


def child_name(runtime_id: str) -> str:
    """Return the name shared by a Runtime's Pod and Service."""
    return f"msess-{runtime_id}"
