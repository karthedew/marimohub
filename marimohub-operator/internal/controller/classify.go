package controller

import (
	"fmt"

	corev1 "k8s.io/api/core/v1"

	"github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
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

// classifyPod inspects a live Pod's init and main container status
// separately, in that order, since the fetcher and marimo are different
// workloads whose failures must never be conflated: a fetcher exit code
// means something entirely different from a marimo exit code, and the main
// container never even starts until every init container has already
// succeeded.
func classifyPod(pod *corev1.Pod) podVerdict {
	for _, cs := range pod.Status.InitContainerStatuses {
		switch {
		case cs.State.Terminated != nil:
			t := cs.State.Terminated
			if t.Reason == containerReasonOOMKilled {
				return podVerdict{kind: verdictFailed, reason: v1alpha1.ReasonOOMKilled, message: "source-fetcher was OOMKilled"}
			}
			if t.ExitCode == 0 {
				continue // fetcher succeeded; evaluate the next container.
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
