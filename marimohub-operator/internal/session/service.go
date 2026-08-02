package session

import (
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"

	"github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
	"github.com/karthedew/marimohub/marimohub-operator/internal/runtimecontract"
)

// BuildService returns the desired Runtime Service for cr. It takes no
// Options: a Runtime Service is pure identity and routing (name, owner,
// selector, fixed port), none of which the chart-wide policy in Options
// influences. Reconciling a Service's selector/ports while preserving its
// ClusterIP, and refusing to adopt a foreign same-name object, are
// reconcile-loop decisions the controller makes by comparing this desired
// object against live cluster state; this function only ever produces the
// desired shape.
func BuildService(cr *v1alpha1.MarimoSession) *corev1.Service {
	return &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{
			Name:            runtimecontract.ChildName(cr.Name),
			Namespace:       cr.Namespace,
			Labels:          childLabels(cr),
			OwnerReferences: []metav1.OwnerReference{ownerReference(cr)},
		},
		Spec: corev1.ServiceSpec{
			Type:     corev1.ServiceTypeClusterIP,
			Selector: map[string]string{runtimecontract.LabelSession: cr.Name},
			Ports: []corev1.ServicePort{
				{
					Name:       "http",
					Port:       runtimecontract.RuntimePort,
					TargetPort: intstr.FromInt32(runtimecontract.RuntimePort),
					Protocol:   corev1.ProtocolTCP,
				},
			},
		},
	}
}
