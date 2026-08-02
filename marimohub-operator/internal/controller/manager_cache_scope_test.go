/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"context"

	"github.com/google/uuid"
	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"

	marimohubv1alpha1 "github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
)

// This proves the exact mechanism cmd/main.go wires up: a manager's cache
// restricted to one configured namespace via cache.Options.DefaultNamespaces
// cannot list or watch a MarimoSession created in a different namespace. It
// builds a real controller-runtime cache against the same envtest API server
// the rest of this suite uses, rather than only asserting the manager
// construction code compiles.
var _ = Describe("namespace-scoped cache", func() {
	It("only observes MarimoSessions in the configured namespace", func() {
		allowed := "runtime-" + uuid.NewString()
		other := "runtime-" + uuid.NewString()
		for _, ns := range []string{allowed, other} {
			Expect(k8sClient.Create(ctx, &corev1.Namespace{
				ObjectMeta: metav1.ObjectMeta{Name: ns},
			})).To(Succeed())
		}

		inAllowed := newValidSession()
		inAllowed.Namespace = allowed
		Expect(k8sClient.Create(ctx, inAllowed)).To(Succeed())

		inOther := newValidSession()
		inOther.Namespace = other
		Expect(k8sClient.Create(ctx, inOther)).To(Succeed())

		scopedCache, err := cache.New(cfg, cache.Options{
			Scheme: scheme.Scheme,
			DefaultNamespaces: map[string]cache.Config{
				allowed: {},
			},
		})
		Expect(err).NotTo(HaveOccurred())

		cacheCtx, cacheCancel := context.WithCancel(ctx)
		defer cacheCancel()
		go func() { _ = scopedCache.Start(cacheCtx) }()
		Expect(scopedCache.WaitForCacheSync(cacheCtx)).To(BeTrue())

		reader, err := scopedCache.GetInformer(cacheCtx, &marimohubv1alpha1.MarimoSession{})
		Expect(err).NotTo(HaveOccurred())
		Expect(reader).NotTo(BeNil())

		var list marimohubv1alpha1.MarimoSessionList
		Expect(scopedCache.List(cacheCtx, &list)).To(Succeed())
		names := make([]string, 0, len(list.Items))
		for _, item := range list.Items {
			names = append(names, item.Namespace+"/"+item.Name)
		}
		Expect(names).To(ConsistOf(allowed + "/" + inAllowed.Name))

		var single marimohubv1alpha1.MarimoSession
		Expect(scopedCache.Get(cacheCtx, client.ObjectKeyFromObject(inOther), &single)).To(HaveOccurred())
	})
})
