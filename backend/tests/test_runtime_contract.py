"""Conformance test: the Python and Go halves of the Runtime naming/status contract agree.

`app/services/runtime_contract.py` and
`marimohub-operator/internal/runtimecontract/names.go` (plus the Condition
type/Reason constants in `marimohub-operator/api/v1alpha1/marimosession_types.go`)
are two independently maintained copies of one cross-language contract. This
test reads both sides directly and fails on drift, rather than checking
either implementation against a third, separately maintained table that
could itself go stale.
"""

import pathlib
import re

from app.services import runtime_contract as contract

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_NAMES_GO = _REPO_ROOT / "marimohub-operator" / "internal" / "runtimecontract" / "names.go"
_TYPES_GO = _REPO_ROOT / "marimohub-operator" / "api" / "v1alpha1" / "marimosession_types.go"


def _go_string_const(source: str, name: str) -> str:
    match = re.search(rf'\b{re.escape(name)}\s*=\s*"([^"]*)"', source)
    assert match is not None, f"could not find Go string const {name!r}"
    return match.group(1)


def _go_int_const(source: str, name: str) -> int:
    match = re.search(rf"\b{re.escape(name)}\s*=\s*(\d+)", source)
    assert match is not None, f"could not find Go int const {name!r}"
    return int(match.group(1))


def test_labels_match_go_runtimecontract() -> None:
    source = _NAMES_GO.read_text()
    assert _go_string_const(source, "LabelNotebook") == contract.LABEL_NOTEBOOK
    assert _go_string_const(source, "LabelWorkspace") == contract.LABEL_WORKSPACE
    assert _go_string_const(source, "LabelMode") == contract.LABEL_MODE
    assert _go_string_const(source, "LabelSession") == contract.LABEL_SESSION


def test_annotations_match_go_runtimecontract() -> None:
    source = _NAMES_GO.read_text()
    assert _go_string_const(source, "AnnotationWakeRequest") == contract.ANNOTATION_WAKE_REQUEST
    assert _go_string_const(source, "AnnotationActivity") == contract.ANNOTATION_ACTIVITY


def test_secret_keys_match_go_runtimecontract() -> None:
    source = _NAMES_GO.read_text()
    assert _go_string_const(source, "SecretKeyMarimoToken") == contract.SECRET_KEY_MARIMO_TOKEN
    assert (
        _go_string_const(source, "SecretKeyRuntimeCredential")
        == contract.SECRET_KEY_RUNTIME_CREDENTIAL
    )


def test_runtime_port_matches_go_runtimecontract() -> None:
    source = _NAMES_GO.read_text()
    assert _go_int_const(source, "RuntimePort") == contract.RUNTIME_PORT


def test_secret_and_child_name_helpers_match_go_naming_shape() -> None:
    source = _NAMES_GO.read_text()
    assert _go_string_const(source, "secretNamePrefix") == "msess-"
    assert _go_string_const(source, "secretNameSuffix") == "-env"
    assert contract.secret_name("abc") == "msess-abc-env"
    assert contract.child_name("abc") == "msess-abc"


def test_condition_types_match_go_marimosession_types() -> None:
    source = _TYPES_GO.read_text()
    assert (
        _go_string_const(source, "ConditionTypeCredentialsAvailable")
        == contract.CONDITION_CREDENTIALS_AVAILABLE
    )
    assert (
        _go_string_const(source, "ConditionTypeCapacityAvailable")
        == contract.CONDITION_CAPACITY_AVAILABLE
    )
    assert _go_string_const(source, "ConditionTypeReady") == contract.CONDITION_READY
    assert _go_string_const(source, "ConditionTypeReconciled") == contract.CONDITION_RECONCILED


def test_reasons_match_go_marimosession_types() -> None:
    source = _TYPES_GO.read_text()
    assert (
        _go_string_const(source, "ReasonCredentialsMissing") == contract.REASON_CREDENTIALS_MISSING
    )
    assert (
        _go_string_const(source, "ReasonCredentialsInvalid") == contract.REASON_CREDENTIALS_INVALID
    )
    assert _go_string_const(source, "ReasonQuotaExceeded") == contract.REASON_QUOTA_EXCEEDED
    assert _go_string_const(source, "ReasonResourceConflict") == contract.REASON_RESOURCE_CONFLICT
    assert _go_string_const(source, "ReasonInvalidSpec") == contract.REASON_INVALID_SPEC
    assert _go_string_const(source, "ReasonImageUnavailable") == contract.REASON_IMAGE_UNAVAILABLE
    assert _go_string_const(source, "ReasonSourceUnavailable") == contract.REASON_SOURCE_UNAVAILABLE
    assert _go_string_const(source, "ReasonRuntimeExited") == contract.REASON_RUNTIME_EXITED
    assert _go_string_const(source, "ReasonOOMKilled") == contract.REASON_OOM_KILLED
    assert (
        _go_string_const(source, "ReasonInfrastructureLost") == contract.REASON_INFRASTRUCTURE_LOST
    )
    assert (
        _go_string_const(source, "ReasonReconcileSucceeded") == contract.REASON_RECONCILE_SUCCEEDED
    )
    assert _go_string_const(source, "ReasonPlatformDenied") == contract.REASON_PLATFORM_DENIED
    assert _go_string_const(source, "ReasonStarting") == contract.REASON_STARTING
    assert _go_string_const(source, "ReasonUnhealthy") == contract.REASON_UNHEALTHY
