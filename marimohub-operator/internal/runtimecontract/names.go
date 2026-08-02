// Package runtimecontract collects the naming and labeling constants shared
// between the operator and the backend for one MarimoSession Runtime. These
// values are a cross-language contract, not an implementation detail: the
// backend authors the labels, annotations, and credential Secret this
// package names, and the operator reads them back under the exact same
// names. A future conformance test reads both the Python and Go sides of
// this contract and fails on drift; that test can only exist once the
// Python-side constants it compares against are introduced, so this package
// is deliberately the only half committed so far.
package runtimecontract

import (
	"fmt"
	"strings"

	corev1 "k8s.io/api/core/v1"
)

const (
	// LabelNotebook, LabelWorkspace, and LabelMode must exactly agree with
	// the identically named spec fields; the CRD's CEL validation rules
	// enforce that agreement at admission time rather than trusting every
	// writer to keep them in sync.
	LabelNotebook  = "marimohub.io/notebook"
	LabelWorkspace = "marimohub.io/workspace"
	LabelMode      = "marimohub.io/mode"

	// LabelSession is applied to every child Pod/Service so the operator can
	// select a Runtime's children without depending on a fixed naming scheme.
	LabelSession = "marimohub.io/session"
)

const (
	// AnnotationWakeRequest carries a unique opaque value the backend gateway
	// writes to ask a sleeping deploy Runtime to start. The operator
	// acknowledges the observed value in status.observedWakeRequest so a
	// repeated reconcile of the same request never starts a second attempt.
	AnnotationWakeRequest = "marimohub.io/wake-request"

	// AnnotationActivity carries a unique opaque value the backend gateway
	// writes on proxied traffic so the operator can reset its idle clock
	// without trusting a client-supplied timestamp.
	AnnotationActivity = "marimohub.io/activity"
)

const (
	// SecretKeyMarimoToken is the marimo `--token-password` value the
	// operator projects into the Runtime container's environment.
	SecretKeyMarimoToken = "MARIMO_TOKEN"

	// SecretKeyRuntimeCredential is the opaque bearer credential the
	// source-fetcher presents to the internal API; see ADR 0001.
	SecretKeyRuntimeCredential = "RUNTIME_CREDENTIAL"
)

// RuntimePort is the fixed port every Runtime container listens on and every
// Runtime Service exposes. It is not configurable: baking in one well-known
// port keeps the Pod/Service builders and the readiness probe free of a
// value that would otherwise have to flow through every layer for no
// operational benefit.
const RuntimePort = 8080

// SecretType is the exact corev1.Secret.Type value the operator accepts for
// an owned credential Secret. A same-name Secret of any other type is
// rejected as invalid regardless of its keys: pinning the type narrows
// "looks like our Secret" to the one shape the backend is documented to
// create, rather than accepting anything that happens to carry the right
// key names.
const SecretType = corev1.SecretTypeOpaque

const (
	secretNamePrefix = "msess-"
	secretNameSuffix = "-env"
)

// SecretName returns the name of a Runtime's owned credential Secret.
func SecretName(runtimeID string) string {
	return fmt.Sprintf("%s%s%s", secretNamePrefix, runtimeID, secretNameSuffix)
}

// RuntimeIDFromSecretName reverses SecretName: given a Secret's name, it
// returns the Runtime ID that name was derived from, or false if the name
// does not have the msess-<id>-env shape at all. The Secret-metadata watch
// mapper is this function's only caller: given a Secret name change event
// for any Secret in the namespace, it has to recover a Runtime ID using
// exactly the same shape SecretName produces, not a second hand-written
// pattern that could quietly drift from it.
func RuntimeIDFromSecretName(name string) (string, bool) {
	// The length check must come before trimming prefix and suffix
	// separately: for a short, pathological input like "msess-env" the
	// prefix and suffix each match on their own but overlap on the shared
	// "-", so trimming them one at a time would strip an overlapping
	// character twice and misread "" as a non-empty leftover ID.
	if len(name) < len(secretNamePrefix)+len(secretNameSuffix) {
		return "", false
	}
	if !strings.HasPrefix(name, secretNamePrefix) || !strings.HasSuffix(name, secretNameSuffix) {
		return "", false
	}
	id := name[len(secretNamePrefix) : len(name)-len(secretNameSuffix)]
	if id == "" {
		return "", false
	}
	return id, true
}

// ChildName returns the name shared by a Runtime's Pod and Service. Pod and
// Service intentionally collide on this one name: a Runtime has at most one
// of each, so there is no reason to mint two names where callers would then
// have to remember which is which.
func ChildName(runtimeID string) string {
	return fmt.Sprintf("msess-%s", runtimeID)
}
