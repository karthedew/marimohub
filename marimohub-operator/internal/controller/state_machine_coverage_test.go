package controller

// This file is documentation, not an executable test: it is the map from
// this controller's own state-machine invariants to the test functions that
// exercise them, kept next to the tests themselves so it cannot drift far
// from what they actually cover. Update it in the same change that adds or
// renames a covering test.
//
// State invariants
// -----------------------------------------------------------------------
// 1.  New CR + missing/invalid Secret -> Pending, CredentialsAvailable=False.
//       TestMarimoSessionController: "stays Pending with CredentialsAvailable=False when the Secret is missing"
//       credential Secret validation: "rejects a Secret owned by a different UID as invalid, not missing"
//       credential Secret validation: "rejects a Secret missing the RUNTIME_CREDENTIAL key"
//       credential Secret validation: "rejects a Secret missing the MARIMO_TOKEN key"
//       credential Secret validation: "rejects a Secret that is not marked immutable"
//       credential Secret validation: "rejects a Secret of the wrong type"
//
// 2.  Valid Secret -> reconcile Service, record a start attempt, Starting, create Pod.
//       TestMarimoSessionController: "creates the Service and Pod, entering Starting, once the Secret is valid"
//       foreign Pod/Service collisions and Service drift: "corrects Service selector/port drift while preserving the ClusterIP"
//
// 3.  Pod passes startup and authenticated readiness -> Ready; lastActivity=now;
//     no idle evaluation in the same reconcile.
//       TestMarimoSessionController: "becomes Ready once the Pod passes authenticated readiness, and does not idle-evaluate in that same reconcile"
//
// 4.  New activity token -> acknowledge in status, lastActivity from the controller clock.
//       idle timers and the wake protocol: "keeps a Runtime alive when an activity token arrives before the deadline, and never moves lastActivity backwards"
//       idle timers and the wake protocol: "treats a repeated activity annotation value as no new activity"
//
// 5.  Idle edit/run -> delete the CR; owner-reference GC removes children.
//       idle timers and the wake protocol: "deletes an idle edit/run CR after its idle timeout elapses"
//     (Owner-reference GC itself does not run in envtest; BuildPod/BuildService's
//     owner references are asserted directly in internal/session's own tests and
//     in the foreign-collision tests here.)
//
// 6.  Idle deploy -> persist Sleeping and clear podName, then delete the Pod;
//     retain CR/Service/Secret; a crash between those writes cannot recreate
//     the Pod as infrastructure recovery.
//       idle timers and the wake protocol: "sleeps (not deletes) an idle deploy Runtime, persisting Sleeping and clearing podName before the Pod is removed"
//       reconcileMissingPod's ordering (podName checked before Phase) is what
//       makes a Sleeping CR's cleared podName safe against being misread as loss;
//       see failure classification and recovery: "recreates a Pod lost to infrastructure..."
//       for the contrasting case where podName is deliberately left set.
//
// 7.  Sleeping deploy + unobserved wake request -> persist Starting, acknowledge,
//     then create the Pod; crash-safe.
//       idle timers and the wake protocol: "wakes a sleeping deploy Runtime on a fresh wake-request token and starts a new Pod"
//       idle timers and the wake protocol: "ignores a stale wake-request value that was already observed"
//       idle timers and the wake protocol: "resumes Pod creation after a crash between persisting Starting and creating the Pod, without requiring the wake annotation again"
//       idle timers and the wake protocol: "does not immediately re-sleep a deploy Runtime that just woke and is still starting"
//
// 8.  Quota rejection -> CapacityAvailable=False/QuotaExceeded, consume the
//     attempt, retain zero compute, no timed retry; only an unobserved wake
//     token or a spec.resources change starts a new attempt.
//       quota confirmation and platform denial: "blocks on a confirmed quota rejection, does not retry automatically, and clears the stale Condition on an explicit retry"
//       failure classification and recovery: "does not replace the Pod when only idleTimeoutSeconds changes" (the negative half: idleTimeoutSeconds never counts as the resources change that would restart an attempt)
//
// 9.  Deterministic workload failure -> persist Failed, remove the Pod, never recreate.
//       failure classification and recovery: "fails RuntimeExited on a non-zero marimo exit and never recreates the Pod afterward"
//       failure classification and recovery: "fails RuntimeExited on a zero exit before ever serving"
//       failure classification and recovery: "fails OOMKilled on the main container"
//       failure classification and recovery: "fails SourceUnavailable on a fetcher auth failure and removes the Pod"
//       failure classification and recovery: "fails ImageUnavailable once a Pod stuck pulling its image exceeds the startup deadline"
//       TestClassifyPod* (internal/controller/classify_test.go) pin every container-status branch in isolation.
//
// 10. Infrastructure Pod loss -> InfrastructureLost, bounded backoff, recreate
//     unless Sleeping, Failed, quota-blocked, or credentials-invalid.
//       failure classification and recovery: "recreates a Pod lost to infrastructure with bounded backoff, waiting for NotFound first"
//       failure classification and recovery: "classifies a node that stops reporting as Unknown and recovers infrastructure rather than failing"
//       Structural guards (never reached from the excluded states) are reviewed
//       directly in reconcileFailedPhase, reconcileSleepingPhase, and
//       reconcileMissingPod's quota-blocked branch, and exercised by:
//       failure classification and recovery: "fails RuntimeExited on a non-zero marimo exit and never recreates the Pod afterward" (Failed guard)
//       idle timers and the wake protocol: "ignores a stale wake-request value that was already observed" (Sleeping guard)
//       quota confirmation and platform denial: "blocks on a confirmed quota rejection..." (quota-blocked guard)
//       credential Secret validation: "clears Ready and removes the Pod..." (credentials-invalid guard)
//
// 11. Secret deletion/mutation -> persist non-Ready credential-invalid state
//     first, then remove a live Pod; edit/run is cleaned up, deploy waits
//     for backend repair plus a new wake request.
//       credential Secret validation: "clears Ready and removes the Pod, in that order, when the Secret is deleted after the Runtime was Ready"
//       credential Secret validation: "re-asserts CredentialsAvailable=True and a cleared message once a missing Secret is repaired"
//       credential Secret validation: "keeps a Ready deploy Runtime Sleeping after Secret repair until an unobserved wake request arrives"
//
// 12. A previously Ready Pod that loses readiness is cleared from routing
//     immediately; unhealthy grace then Failed; Unknown/node-loss instead
//     follows infrastructure recovery on its own deadline.
//       failure classification and recovery: "clears Ready immediately on readiness loss, then fails after the unhealthy timeout without recreating the Pod"
//       failure classification and recovery: "recovers readiness within the grace period without ever failing"
//       failure classification and recovery: "classifies a node that stops reporting as Unknown and recovers infrastructure rather than failing"
//
// 13. CR deletion -> return immediately; no finalizer, no controller cleanup write.
//       CR deletion and status write conflicts: "returns immediately for a CR with a deletionTimestamp, performing no status write"
//
// Failure Classification table
// -----------------------------------------------------------------------
// Pod deleted/node lost/eviction/preemption -> bounded recreate:
//   invariant 10 tests above.
// source API/network 5xx or timeout -> SourceUnavailable, no Pod recreation:
//   TestClassifyPodFetcherExitCodes (exit 20); invariant 9 tests above.
// quota admission denial -> block until explicit retry:
//   invariant 8 tests above.
// RBAC/SCC/admission policy denial -> fail, no quota mapping:
//   quota confirmation and platform denial: "does not confirm quota exhaustion from Forbidden text alone when no live quota agrees"
//   quota confirmation and platform denial: "fails PlatformDenied on a non-quota Forbidden response (RBAC/SCC/webhook) without ever mapping it to QuotaExceeded"
// image pull unavailable -> transient until deadline, then ImageUnavailable:
//   failure classification and recovery: "fails ImageUnavailable once a Pod stuck pulling its image exceeds the startup deadline"
//   TestClassifyPodInitContainerImagePullIsTransient
// invalid image/command, source 401/403/404 -> deterministic, no retry:
//   TestClassifyPodInvalidImageOrCommandIsDeterministic
//   TestClassifyPodFetcherExitCodes (exit 10, 11)
// init/main non-zero exit -> deterministic, no retry:
//   TestClassifyPodMainContainerNonZeroExitIsDeterministic; invariant 9 tests above.
// OOMKilled -> deterministic, no retry:
//   TestClassifyPodInitContainerOOMKilled, TestClassifyPodMainContainerOOMKilled;
//   failure classification and recovery: "fails OOMKilled on the main container"
// zero exit before serving -> deterministic, no retry:
//   TestClassifyPodMainContainerZeroExitBeforeServingIsDeterministic;
//   failure classification and recovery: "fails RuntimeExited on a zero exit before ever serving"
//
// Additional required scenarios
// -----------------------------------------------------------------------
// Service/Pod foreign collisions and drift:
//   foreign Pod/Service collisions and Service drift (all specs)
// Resource update replacement, idle update without replacement, observedGeneration timing:
//   failure classification and recovery: "replaces the Pod on a spec.resources change and reports progress through Conditions"
//   failure classification and recovery: "does not replace the Pod when only idleTimeoutSeconds changes"
// Status conflicts:
//   CR deletion and status write conflicts: "retries a status write through a conflicting concurrent update and lands both changes"
// Manager restart idempotence:
//   idle timers and the wake protocol: "resumes Pod creation after a crash between persisting Starting and creating the Pod, without requiring the wake annotation again"
// Namespace isolation:
//   manager_cache_scope_test.go: "only observes MarimoSessions in the configured namespace"
// status.message tracks the authoritative Conditions rather than being a
// second source of truth: populated while a Condition blocks Ready, cleared
// once Ready has no active blocking Condition.
//   TestMarimoSessionController: "stays Pending with CredentialsAvailable=False when the Secret is missing" (populated)
//   TestMarimoSessionController: "becomes Ready once the Pod passes authenticated readiness..." (cleared)
// Event-driven enqueue behavior (spec/annotation watch, status-write suppression, Secret metadata mapping):
//   TestSpecOrWakeActivityChangedFiresOnGenerationChange
//   TestSpecOrWakeActivityChangedFiresOnWakeAnnotationChange
//   TestSpecOrWakeActivityChangedFiresOnActivityAnnotationChange
//   TestSpecOrWakeActivityChangedIgnoresStatusOnlyUpdate
//   TestMapSecretToSessionMapsOwnedSecretName
//   TestMapSecretToSessionIgnoresUnrelatedSecretNames
