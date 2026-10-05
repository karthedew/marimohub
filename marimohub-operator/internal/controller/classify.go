package controller

import (
	"fmt"
	"slices"

	corev1 "k8s.io/api/core/v1"

	"github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
	"github.com/karthedew/marimohub/marimohub-operator/internal/session"
)

// verdictKind is the pure classification of a live Pod's current container
// and condition state. It says nothing about elapsed time or the CR's own
// history -- the reconciler layers deadlines (image pull, unhealthy grace,
// node-loss grace) and "was this previously Ready" on top of it, since
// those need the controller's clock and the CR's own status, neither of
// which belongs in a function this easy to unit test in isolation.
type verdictKind int

const (
	// verdictStarting is not-yet-resolved: init or main containers are still
	// pulling, starting, or waiting out their own probes normally.
	verdictStarting verdictKind = iota
	// verdictReady means the main container reports Ready.
	verdictReady
	// verdictNotReady means the main container is Running but its readiness
	// probe is currently failing. The reconciler decides what this means:
	// ordinary startup if the Runtime was never Ready before, or a
	// readiness-loss regression subject to the unhealthy timeout if it was.
	verdictNotReady
	// verdictNodeUnknown means the kubelet is not reporting this Pod's
	// status at all (Ready condition Unknown), the signature of a lost or
	// unreachable node rather than a workload problem.
	verdictNodeUnknown
	// verdictInfrastructureLost means the Pod is going away for a reason
	// outside the workload: it is being deleted, or the platform evicted or
	// otherwise disrupted it. Its container states then only say how it was
	// stopped (exit 143 on SIGTERM, or a clean 0), never how the workload
	// behaved, so they are not classified at all.
	verdictInfrastructureLost
	// verdictImagePulling means a container is waiting on its image; still
	// within Kubernetes' own pull backoff, subject to the operator's
	// configured startup deadline.
	verdictImagePulling
	// verdictFailed is a deterministic, terminal verdict: reason and message
	// are set and the Pod must be removed without ever being recreated.
	verdictFailed
)

type podVerdict struct {
	kind    verdictKind
	reason  string
	message string
}

// containerReasonOOMKilled is the container status Reason the kubelet
// reports for a container terminated by the Linux OOM killer, checked
// identically for the init and main container.
const containerReasonOOMKilled = "OOMKilled"

// podReasonEvicted is the Pod status Reason the kubelet sets on a Pod it
// evicts under node pressure. It leaves that Pod behind in the Failed phase,
// with no deletionTimestamp, until something deletes it.
const podReasonEvicted = "Evicted"

// classifyPod inspects a live Pod's init and main container status
// separately, in that order, since the fetcher and marimo are different
// workloads whose failures must never be conflated: a fetcher exit code
// means something entirely different from a marimo exit code, and the main
// container never even starts until every init container has already
// succeeded. A Pod that is going away is checked before either, because
// stopping it terminates whichever container is running, and that exit
// would otherwise read as the workload failing.
func classifyPod(pod *corev1.Pod) podVerdict {
	if podGoingAway(pod) {
		return podVerdict{kind: verdictInfrastructureLost}
	}

	for _, cs := range pod.Status.InitContainerStatuses {
		switch {
		case cs.State.Terminated != nil:
			t := cs.State.Terminated
			if t.Reason == containerReasonOOMKilled {
				return podVerdict{kind: verdictFailed, reason: v1alpha1.ReasonOOMKilled, message: cs.Name + " was OOMKilled"}
			}
			if t.ExitCode == 0 {
				continue // this init container succeeded; evaluate the next one.
			}
			if cs.Name == session.WorkspaceInitContainerName {
				return classifyWorkspaceInitExit(t.ExitCode, t.Message)
			}
			return classifyFetcherExit(t.ExitCode, t.Message)
		case cs.State.Waiting != nil:
			if v, ok := classifyWaitingReason(cs.State.Waiting.Reason); ok {
				return v
			}
			return podVerdict{kind: verdictStarting}
		default:
			return podVerdict{kind: verdictStarting}
		}
	}

	if len(pod.Status.ContainerStatuses) == 0 {
		return podVerdict{kind: verdictStarting}
	}
	main := pod.Status.ContainerStatuses[0]
	switch {
	case main.State.Terminated != nil:
		t := main.State.Terminated
		if t.Reason == containerReasonOOMKilled {
			return podVerdict{kind: verdictFailed, reason: v1alpha1.ReasonOOMKilled, message: "marimo container was OOMKilled"}
		}
		return podVerdict{
			kind:    verdictFailed,
			reason:  v1alpha1.ReasonRuntimeExited,
			message: fmt.Sprintf("marimo container exited with code %d", t.ExitCode),
		}
	case main.State.Waiting != nil:
		if v, ok := classifyWaitingReason(main.State.Waiting.Reason); ok {
			return v
		}
		return podVerdict{kind: verdictStarting}
	case main.State.Running != nil:
		return readinessVerdict(pod, main)
	default:
		return podVerdict{kind: verdictStarting}
	}
}

// podGoingAway reports whether pod is being deleted, or was evicted or
// otherwise disrupted. Kubernetes adds a DisruptionTarget condition before
// every such disruption: eviction API, preemption, taint-based deletion,
// Pod garbage collection, and kubelet-initiated termination such as
// node-pressure eviction or node shutdown. The Evicted reason also covers a
// kubelet that does not set that condition. A disruption the control plane
// abandons leaves the condition False, and the Pod then counts as live again.
func podGoingAway(pod *corev1.Pod) bool {
	if !pod.DeletionTimestamp.IsZero() || pod.Status.Reason == podReasonEvicted {
		return true
	}
	return slices.ContainsFunc(pod.Status.Conditions, func(c corev1.PodCondition) bool {
		return c.Type == corev1.DisruptionTarget && c.Status == corev1.ConditionTrue
	})
}

// classifyWaitingReason distinguishes a container still legitimately
// waiting on its image (transient, subject to a deadline) from one that
// will never start no matter how long it waits (a malformed image
// reference or command, a deterministic configuration mistake rather than a
// registry or network hiccup).
func classifyWaitingReason(reason string) (podVerdict, bool) {
	switch reason {
	case "ImagePullBackOff", "ErrImagePull", "ImageInspectError", "RegistryUnavailable":
		return podVerdict{kind: verdictImagePulling}, true
	case "InvalidImageName", "CreateContainerConfigError", "CreateContainerError":
		return podVerdict{
			kind:    verdictFailed,
			reason:  v1alpha1.ReasonInvalidSpec,
			message: "container could not be created: " + reason,
		}, true
	default:
		return podVerdict{}, false
	}
}

// classifyFetcherExit maps images/source-fetcher's stable exit-code
// contract onto a verdict. Every non-zero code the fetcher can return
// already reflects its own bounded retries against transient failures (see
// its curl --retry usage), so from the Pod's perspective every one of these
// is final: there is no separate "retry the fetch" step left for the
// operator to drive.
func classifyFetcherExit(exitCode int32, message string) podVerdict {
	if exitCode == 40 {
		// Fetcher misconfiguration (a required env var or mounted file is
		// missing) is a Pod/chart wiring bug, not something wrong with this
		// Runtime's source -- InvalidSpec says so distinctly from
		// SourceUnavailable.
		return podVerdict{kind: verdictFailed, reason: v1alpha1.ReasonInvalidSpec, message: "source-fetcher misconfiguration: " + message}
	}
	return podVerdict{kind: verdictFailed, reason: v1alpha1.ReasonSourceUnavailable, message: "source fetch failed: " + message}
}

// classifyWorkspaceInitExit treats any failure to create the Workspace
// directory as InvalidSpec: the only ways mkdir fails here are a missing
// workspaces/ directory or one the Runtime group cannot write, both of
// which are storage provisioning mistakes that retrying will not fix and
// that have nothing to do with this Runtime's Notebook source.
func classifyWorkspaceInitExit(exitCode int32, message string) podVerdict {
	return podVerdict{
		kind:    verdictFailed,
		reason:  v1alpha1.ReasonInvalidSpec,
		message: fmt.Sprintf("workspace directory could not be prepared (exit %d): %s", exitCode, message),
	}
}

// readinessVerdict distinguishes a genuinely unreachable node (kubelet not
// reporting, Ready condition Unknown) from an ordinary readiness-probe
// failure on a Pod the kubelet is actively and successfully observing.
func readinessVerdict(pod *corev1.Pod, main corev1.ContainerStatus) podVerdict {
	for _, c := range pod.Status.Conditions {
		if c.Type == corev1.PodReady && c.Status == corev1.ConditionUnknown {
			return podVerdict{kind: verdictNodeUnknown}
		}
	}
	if main.Ready {
		return podVerdict{kind: verdictReady}
	}
	return podVerdict{kind: verdictNotReady}
}
