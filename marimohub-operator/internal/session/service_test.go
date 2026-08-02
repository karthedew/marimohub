package session

import (
	"reflect"
	"testing"

	corev1 "k8s.io/api/core/v1"

	"github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
	"github.com/karthedew/marimohub/marimohub-operator/internal/runtimecontract"
)

// TestBuildService pins the Service contract across every mode: ClusterIP,
// owner-referenced, fixed at the Runtime port, and selecting only this
// Runtime's children.
func TestBuildService(t *testing.T) {
	modes := []v1alpha1.RuntimeMode{v1alpha1.RuntimeModeEdit, v1alpha1.RuntimeModeRun, v1alpha1.RuntimeModeDeploy}
	for _, mode := range modes {
		t.Run(string(mode), func(t *testing.T) {
			cr := testSession(mode, ubuntuImage)
			svc := BuildService(cr)

			if got, want := svc.Name, runtimecontract.ChildName(cr.Name); got != want {
				t.Errorf("Name = %q, want %q", got, want)
			}
			if svc.Namespace != cr.Namespace {
				t.Errorf("Namespace = %q, want %q", svc.Namespace, cr.Namespace)
			}
			if svc.Spec.Type != corev1.ServiceTypeClusterIP {
				t.Errorf("Type = %q, want ClusterIP", svc.Spec.Type)
			}

			wantSelector := map[string]string{runtimecontract.LabelSession: cr.Name}
			if !reflect.DeepEqual(svc.Spec.Selector, wantSelector) {
				t.Errorf("Selector = %v, want %v", svc.Spec.Selector, wantSelector)
			}

			if len(svc.Spec.Ports) != 1 {
				t.Fatalf("Ports = %d entries, want 1", len(svc.Spec.Ports))
			}
			port := svc.Spec.Ports[0]
			if port.Port != runtimecontract.RuntimePort {
				t.Errorf("Port = %d, want %d", port.Port, runtimecontract.RuntimePort)
			}
			if port.TargetPort.IntValue() != runtimecontract.RuntimePort {
				t.Errorf("TargetPort = %d, want %d", port.TargetPort.IntValue(), runtimecontract.RuntimePort)
			}
			if port.Protocol != corev1.ProtocolTCP {
				t.Errorf("Protocol = %q, want TCP", port.Protocol)
			}

			checkOwnerReference(t, cr, svc.OwnerReferences)

			wantLabels := map[string]string{
				runtimecontract.LabelNotebook:  testNotebookID,
				runtimecontract.LabelWorkspace: testWorkspaceID,
				runtimecontract.LabelMode:      string(mode),
				runtimecontract.LabelSession:   testName,
			}
			if !reflect.DeepEqual(svc.Labels, wantLabels) {
				t.Errorf("Labels = %v, want %v", svc.Labels, wantLabels)
			}
		})
	}
}
