package controller

import (
	"context"
	"maps"
	"strings"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// quotaAdmissionMarker is the substring Kubernetes' own ResourceQuota
// admission plugin includes in every rejection it produces. Relying on it
// is deliberate and narrow: a Forbidden response is otherwise ambiguous
// between quota, RBAC, SCC, LimitRange, Pod Security, and an arbitrary
// validating webhook, none of which return a structured reason a client can
// switch on. Requiring this exact text, on top of independently confirming
// exhaustion against live quota objects below, means the operator never
// classifies a denial as QuotaExceeded on text pattern-matching alone.
const quotaAdmissionMarker = "exceeded quota:"

// confirmQuotaExhaustion decides whether a Pod-create Forbidden response
// was actually caused by a namespace ResourceQuota, as opposed to any other
// admission denial that happens to also return 403 Forbidden. It requires
// both signals to agree: the canonical quota-admission error text, and an
// independent read of every live quota in the namespace showing the Pod's
// own effective request would in fact exceed at least one of them. Neither
// signal alone is trusted, because the error text is a string Kubernetes
// does not guarantee never to reuse elsewhere, and quota headroom by itself
// says nothing about why any particular request was rejected.
func (r *MarimoSessionReconciler) confirmQuotaExhaustion(ctx context.Context, namespace string, pod *corev1.Pod, createErr error) (bool, error) {
	if !strings.Contains(createErr.Error(), quotaAdmissionMarker) {
		return false, nil
	}

	var quotas corev1.ResourceQuotaList
	if err := r.List(ctx, &quotas, client.InNamespace(namespace)); err != nil {
		return false, err
	}

	request := effectivePodRequest(pod)
	for _, q := range quotas.Items {
		for name, hard := range q.Status.Hard {
			reqVal, tracked := request[name]
			if !tracked {
				continue
			}
			used := q.Status.Used[name]
			projected := used.DeepCopy()
			projected.Add(reqVal)
			if projected.Cmp(hard) > 0 {
				return true, nil
			}
		}
	}
	return false, nil
}

// effectivePodRequest computes the resource quantities a ResourceQuota
// admission check would compare against a Pod, in the exact shape
// ResourceQuota.Status.Hard/Used key their entries under: "requests.<name>"
// and "limits.<name>" for every resource dimension, plus a flat "pods": 1.
// Kubernetes computes a Pod's effective per-resource request as the greater
// of the sum across regular containers and the maximum across init
// containers (only one init container ever runs at a time, so init
// containers never add to each other) -- not a naive sum of every
// container, which would overcount whenever the fetcher's own footprint is
// smaller than the Runtime container's, as it always is here.
func effectivePodRequest(pod *corev1.Pod) corev1.ResourceList {
	out := corev1.ResourceList{corev1.ResourcePods: resource.MustParse("1")}
	for prefix, list := range map[string]corev1.ResourceList{
		"requests.": effectiveResourceList(pod, func(c corev1.Container) corev1.ResourceList { return c.Resources.Requests }),
		"limits.":   effectiveResourceList(pod, func(c corev1.Container) corev1.ResourceList { return c.Resources.Limits }),
	} {
		for name, qty := range list {
			out[corev1.ResourceName(prefix+string(name))] = qty
		}
	}
	return out
}

func effectiveResourceList(pod *corev1.Pod, sel func(corev1.Container) corev1.ResourceList) corev1.ResourceList {
	sum := sumResourceLists(pod.Spec.Containers, sel)
	maxInit := maxResourceLists(pod.Spec.InitContainers, sel)

	result := corev1.ResourceList{}
	maps.Copy(result, sum)
	for name, qty := range maxInit {
		if current, ok := result[name]; !ok || qty.Cmp(current) > 0 {
			result[name] = qty
		}
	}
	return result
}

func sumResourceLists(containers []corev1.Container, sel func(corev1.Container) corev1.ResourceList) corev1.ResourceList {
	total := corev1.ResourceList{}
	for _, c := range containers {
		for name, qty := range sel(c) {
			cur := total[name]
			cur.Add(qty)
			total[name] = cur
		}
	}
	return total
}

func maxResourceLists(containers []corev1.Container, sel func(corev1.Container) corev1.ResourceList) corev1.ResourceList {
	result := corev1.ResourceList{}
	for _, c := range containers {
		for name, qty := range sel(c) {
			if cur, ok := result[name]; !ok || qty.Cmp(cur) > 0 {
				result[name] = qty
			}
		}
	}
	return result
}

// sanitizeAdmissionError extracts the human-readable tail of an admission
// error, stripping the "<kind> \"<name>\" is forbidden: " prefix Kubernetes
// always adds. status.message must never carry an API protocol prefix; this
// is the one place that strips it before the message ever reaches a
// statusPatch.
func sanitizeAdmissionError(err error) string {
	msg := err.Error()
	if _, after, found := strings.Cut(msg, "forbidden: "); found {
		return after
	}
	return msg
}
