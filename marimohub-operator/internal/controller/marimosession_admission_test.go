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
	"strings"

	"github.com/google/uuid"
	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"

	marimohubv1alpha1 "github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
	"github.com/karthedew/marimohub/marimohub-operator/internal/runtimecontract"
)

// testNamespace is the fixed namespace every admission test creates objects
// in; envtest has no notion of the chart-provided Runtime namespace name, so
// any valid namespace string works here.
const testNamespace = "default"

// validImage is a digest-qualified reference satisfying the CRD's Pattern
// marker; the exact repository name is immaterial to admission, only the
// "name@sha256:<64 hex>" shape is.
var validImage = "ghcr.io/karthedew/marimohub-runtime-ubi@sha256:" + strings.Repeat("0123456789abcdef", 4)

// keyOf returns the client.Get/client.Delete key for a MarimoSession.
func keyOf(s *marimohubv1alpha1.MarimoSession) types.NamespacedName {
	return types.NamespacedName{Name: s.Name, Namespace: s.Namespace}
}

// newValidSession builds a MarimoSession that satisfies every admission rule:
// a UUID name, matching labels/spec identity, a digest-qualified image, and a
// safe baseUrl. Each call mints fresh UUIDs so parallel It blocks never
// collide on metadata.name.
func newValidSession() *marimohubv1alpha1.MarimoSession {
	name := uuid.NewString()
	notebookID := uuid.NewString()
	workspaceID := uuid.NewString()
	return &marimohubv1alpha1.MarimoSession{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: testNamespace,
			Labels: map[string]string{
				runtimecontract.LabelNotebook:  notebookID,
				runtimecontract.LabelWorkspace: workspaceID,
				runtimecontract.LabelMode:      string(marimohubv1alpha1.RuntimeModeEdit),
			},
		},
		Spec: marimohubv1alpha1.MarimoSessionSpec{
			NotebookID:  notebookID,
			WorkspaceID: workspaceID,
			Mode:        marimohubv1alpha1.RuntimeModeEdit,
			Image:       validImage,
			BaseURL:     "/api/proxy/" + name,
		},
	}
}

// asDeploy retargets a valid edit session at deploy mode with a matching
// mode label and a deploymentRevision, since deploy mode requires one.
func asDeploy(s *marimohubv1alpha1.MarimoSession, revision int64) *marimohubv1alpha1.MarimoSession {
	s.Spec.Mode = marimohubv1alpha1.RuntimeModeDeploy
	s.Labels[runtimecontract.LabelMode] = string(marimohubv1alpha1.RuntimeModeDeploy)
	s.Spec.DeploymentRevision = &revision
	s.Spec.BaseURL = "/api/deployments/" + s.Name
	return s
}

var _ = Describe("MarimoSession admission", func() {
	Describe("required fields", func() {
		It("rejects a MarimoSession with an empty spec", func() {
			s := &marimohubv1alpha1.MarimoSession{
				ObjectMeta: metav1.ObjectMeta{Name: uuid.NewString(), Namespace: testNamespace},
			}
			err := k8sClient.Create(ctx, s)
			Expect(err).To(HaveOccurred())
		})

		It("accepts a MarimoSession with only the required fields set", func() {
			s := newValidSession()
			Expect(k8sClient.Create(ctx, s)).To(Succeed())
			DeferCleanup(func() { _ = k8sClient.Delete(ctx, s) })

			got := &marimohubv1alpha1.MarimoSession{}
			Expect(k8sClient.Get(ctx, keyOf(s), got)).To(Succeed())
			Expect(got.Spec.CreatorID).To(BeNil())
			Expect(got.Spec.IdleTimeoutSeconds).To(BeNil())
			Expect(got.Spec.Resources).To(BeNil())
			Expect(got.Spec.DeploymentRevision).To(BeNil())
		})
	})

	Describe("idle timeout floor", func() {
		It("rejects an idleTimeoutSeconds below 30", func() {
			s := newValidSession()
			below := int32(29)
			s.Spec.IdleTimeoutSeconds = &below
			Expect(k8sClient.Create(ctx, s)).To(HaveOccurred())
		})

		It("accepts an idleTimeoutSeconds of exactly 30", func() {
			s := newValidSession()
			floor := int32(30)
			s.Spec.IdleTimeoutSeconds = &floor
			Expect(k8sClient.Create(ctx, s)).To(Succeed())
			DeferCleanup(func() { _ = k8sClient.Delete(ctx, s) })
		})
	})

	Describe("mode/revision coupling", func() {
		It("rejects deploy mode without a deploymentRevision", func() {
			s := asDeploy(newValidSession(), 0)
			s.Spec.DeploymentRevision = nil
			Expect(k8sClient.Create(ctx, s)).To(HaveOccurred())
		})

		It("accepts deploy mode with a deploymentRevision", func() {
			s := asDeploy(newValidSession(), 1)
			Expect(k8sClient.Create(ctx, s)).To(Succeed())
			DeferCleanup(func() { _ = k8sClient.Delete(ctx, s) })
		})

		It("rejects edit mode with a deploymentRevision set", func() {
			s := newValidSession()
			revision := int64(1)
			s.Spec.DeploymentRevision = &revision
			Expect(k8sClient.Create(ctx, s)).To(HaveOccurred())
		})

		It("rejects run mode with a deploymentRevision set", func() {
			s := newValidSession()
			s.Spec.Mode = marimohubv1alpha1.RuntimeModeRun
			s.Labels[runtimecontract.LabelMode] = string(marimohubv1alpha1.RuntimeModeRun)
			revision := int64(1)
			s.Spec.DeploymentRevision = &revision
			Expect(k8sClient.Create(ctx, s)).To(HaveOccurred())
		})
	})

	Describe("label and spec identity agreement", func() {
		It("rejects a notebook label that disagrees with spec.notebookId", func() {
			s := newValidSession()
			s.Labels[runtimecontract.LabelNotebook] = uuid.NewString()
			Expect(k8sClient.Create(ctx, s)).To(HaveOccurred())
		})

		It("rejects a workspace label that disagrees with spec.workspaceId", func() {
			s := newValidSession()
			s.Labels[runtimecontract.LabelWorkspace] = uuid.NewString()
			Expect(k8sClient.Create(ctx, s)).To(HaveOccurred())
		})

		It("rejects a mode label that disagrees with spec.mode", func() {
			s := newValidSession()
			s.Labels[runtimecontract.LabelMode] = string(marimohubv1alpha1.RuntimeModeRun)
			Expect(k8sClient.Create(ctx, s)).To(HaveOccurred())
		})

		It("rejects a missing required label", func() {
			s := newValidSession()
			delete(s.Labels, runtimecontract.LabelWorkspace)
			Expect(k8sClient.Create(ctx, s)).To(HaveOccurred())
		})
	})

	Describe("metadata.name UUID validation", func() {
		It("rejects a non-UUID name", func() {
			s := newValidSession()
			s.Name = "not-a-uuid"
			s.Spec.BaseURL = "/api/proxy/not-a-uuid"
			Expect(k8sClient.Create(ctx, s)).To(HaveOccurred())
		})
	})

	Describe("digest-qualified image", func() {
		It("rejects a mutable tag reference", func() {
			s := newValidSession()
			s.Spec.Image = "ghcr.io/karthedew/marimohub-runtime-ubi:latest"
			Expect(k8sClient.Create(ctx, s)).To(HaveOccurred())
		})

		It("rejects a bare repository with no tag or digest", func() {
			s := newValidSession()
			s.Spec.Image = "ghcr.io/karthedew/marimohub-runtime-ubi"
			Expect(k8sClient.Create(ctx, s)).To(HaveOccurred())
		})
	})

	Describe("bounded safe baseUrl", func() {
		It("rejects a relative path", func() {
			s := newValidSession()
			s.Spec.BaseURL = "relative/path"
			Expect(k8sClient.Create(ctx, s)).To(HaveOccurred())
		})

		It("rejects parent-directory traversal", func() {
			s := newValidSession()
			s.Spec.BaseURL = "/api/../secret"
			Expect(k8sClient.Create(ctx, s)).To(HaveOccurred())
		})

		It("rejects a query string", func() {
			s := newValidSession()
			s.Spec.BaseURL = "/api/proxy?x=1"
			Expect(k8sClient.Create(ctx, s)).To(HaveOccurred())
		})

		It("rejects a fragment", func() {
			s := newValidSession()
			s.Spec.BaseURL = "/api/proxy#frag"
			Expect(k8sClient.Create(ctx, s)).To(HaveOccurred())
		})

		It("rejects a baseUrl longer than 256 characters", func() {
			s := newValidSession()
			s.Spec.BaseURL = "/" + strings.Repeat("a", 300)
			Expect(k8sClient.Create(ctx, s)).To(HaveOccurred())
		})
	})

	Describe("launch field immutability on update", func() {
		var s *marimohubv1alpha1.MarimoSession

		BeforeEach(func() {
			s = newValidSession()
			Expect(k8sClient.Create(ctx, s)).To(Succeed())
			DeferCleanup(func() { _ = k8sClient.Delete(ctx, s) })
		})

		It("rejects changing notebookId", func() {
			s.Spec.NotebookID = uuid.NewString()
			Expect(k8sClient.Update(ctx, s)).To(HaveOccurred())
		})

		It("rejects changing workspaceId", func() {
			s.Spec.WorkspaceID = uuid.NewString()
			Expect(k8sClient.Update(ctx, s)).To(HaveOccurred())
		})

		It("rejects changing creatorId", func() {
			creator := uuid.NewString()
			s.Spec.CreatorID = &creator
			Expect(k8sClient.Update(ctx, s)).To(HaveOccurred())
		})

		It("rejects changing mode", func() {
			s.Spec.Mode = marimohubv1alpha1.RuntimeModeRun
			Expect(k8sClient.Update(ctx, s)).To(HaveOccurred())
		})

		It("rejects changing image", func() {
			s.Spec.Image = "ghcr.io/karthedew/marimohub-runtime-ubi@sha256:" + strings.Repeat("f", 64)
			Expect(k8sClient.Update(ctx, s)).To(HaveOccurred())
		})

		It("rejects changing baseUrl", func() {
			s.Spec.BaseURL = "/api/proxy/changed"
			Expect(k8sClient.Update(ctx, s)).To(HaveOccurred())
		})

		It("rejects changing deploymentRevision", func() {
			deploy := asDeploy(newValidSession(), 1)
			Expect(k8sClient.Create(ctx, deploy)).To(Succeed())
			DeferCleanup(func() { _ = k8sClient.Delete(ctx, deploy) })

			newRevision := int64(2)
			deploy.Spec.DeploymentRevision = &newRevision
			Expect(k8sClient.Update(ctx, deploy)).To(HaveOccurred())
		})

		It("allows changing idleTimeoutSeconds", func() {
			timeout := int32(60)
			s.Spec.IdleTimeoutSeconds = &timeout
			Expect(k8sClient.Update(ctx, s)).To(Succeed())
		})
	})
})
