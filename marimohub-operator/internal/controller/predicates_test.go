package controller

import (
	"context"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/event"

	marimohubv1alpha1 "github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
	"github.com/karthedew/marimohub/marimohub-operator/internal/runtimecontract"
)

// testWakeToken is an arbitrary opaque wake-request annotation value; its
// content is never inspected, only its identity against the previously
// observed value.
const testWakeToken = "tok-1"

func sessionWithGenAndAnnotations(gen int64, annotations map[string]string) *marimohubv1alpha1.MarimoSession {
	return &marimohubv1alpha1.MarimoSession{
		ObjectMeta: metav1.ObjectMeta{Generation: gen, Annotations: annotations},
	}
}

func TestSpecOrWakeActivityChangedFiresOnGenerationChange(t *testing.T) {
	p := specOrWakeActivityChanged()
	old := sessionWithGenAndAnnotations(1, nil)
	newObj := sessionWithGenAndAnnotations(2, nil)
	if !p.Update(event.UpdateEvent{ObjectOld: old, ObjectNew: newObj}) {
		t.Error("Update() = false, want true for a generation change")
	}
}

func TestSpecOrWakeActivityChangedFiresOnWakeAnnotationChange(t *testing.T) {
	p := specOrWakeActivityChanged()
	old := sessionWithGenAndAnnotations(1, nil)
	newObj := sessionWithGenAndAnnotations(1, map[string]string{runtimecontract.AnnotationWakeRequest: testWakeToken})
	if !p.Update(event.UpdateEvent{ObjectOld: old, ObjectNew: newObj}) {
		t.Error("Update() = false, want true for a wake-request annotation change with unchanged generation")
	}
}

func TestSpecOrWakeActivityChangedFiresOnActivityAnnotationChange(t *testing.T) {
	p := specOrWakeActivityChanged()
	old := sessionWithGenAndAnnotations(1, map[string]string{runtimecontract.AnnotationActivity: "a"})
	newObj := sessionWithGenAndAnnotations(1, map[string]string{runtimecontract.AnnotationActivity: "b"})
	if !p.Update(event.UpdateEvent{ObjectOld: old, ObjectNew: newObj}) {
		t.Error("Update() = false, want true for an activity annotation change with unchanged generation")
	}
}

// TestSpecOrWakeActivityChangedIgnoresStatusOnlyUpdate is the predicate half
// of suppressing reconcile loops caused only by the controller's own status
// writes: a status subresource Update leaves generation and annotations
// completely untouched, which is exactly what this update event models.
func TestSpecOrWakeActivityChangedIgnoresStatusOnlyUpdate(t *testing.T) {
	p := specOrWakeActivityChanged()
	annotations := map[string]string{runtimecontract.AnnotationWakeRequest: testWakeToken}
	old := sessionWithGenAndAnnotations(1, annotations)
	newObj := sessionWithGenAndAnnotations(1, annotations)
	newObj.Status.Phase = marimohubv1alpha1.RuntimePhaseReady
	if p.Update(event.UpdateEvent{ObjectOld: old, ObjectNew: newObj}) {
		t.Error("Update() = true, want false for a status-only change")
	}
}

// partialMeta builds the same metadata-only object shape the Secret watch
// mapper actually receives at runtime (see builder.OnlyMetadata in
// SetupWithManager), so this test exercises mapSecretToSession against the
// real accessor methods it depends on rather than a structured *corev1.Secret
// it will never actually see.
func partialMeta(name, namespace string) *metav1.PartialObjectMetadata {
	return &metav1.PartialObjectMetadata{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace}}
}

func TestMapSecretToSessionMapsOwnedSecretName(t *testing.T) {
	r := &MarimoSessionReconciler{}
	reqs := r.mapSecretToSession(context.TODO(), partialMeta("msess-abc123-env", "marimohub-sessions"))
	if len(reqs) != 1 {
		t.Fatalf("mapSecretToSession() returned %d requests, want 1", len(reqs))
	}
	if reqs[0].Name != "abc123" || reqs[0].Namespace != "marimohub-sessions" {
		t.Errorf("mapSecretToSession() = %+v", reqs[0])
	}
}

func TestMapSecretToSessionIgnoresUnrelatedSecretNames(t *testing.T) {
	r := &MarimoSessionReconciler{}
	if reqs := r.mapSecretToSession(context.TODO(), partialMeta("some-other-secret", "marimohub-sessions")); reqs != nil {
		t.Errorf("mapSecretToSession() = %+v, want nil", reqs)
	}
}
