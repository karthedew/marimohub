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

package main

import (
	"flag"
	"fmt"
	"os"

	// Import all Kubernetes client auth plugins (e.g. Azure, GCP, OIDC, etc.)
	// to ensure that exec-entrypoint and run can make use of them.
	_ "k8s.io/client-go/plugin/pkg/client/auth"

	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	realclock "k8s.io/utils/clock"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	marimohubv1alpha1 "github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
	"github.com/karthedew/marimohub/marimohub-operator/internal/controller"
	"github.com/karthedew/marimohub/marimohub-operator/internal/managerconfig"
	"github.com/karthedew/marimohub/marimohub-operator/internal/session"
	// +kubebuilder:scaffold:imports
)

var (
	scheme   = runtime.NewScheme()
	setupLog = ctrl.Log.WithName("setup")
)

func init() {
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))

	utilruntime.Must(marimohubv1alpha1.AddToScheme(scheme))
	// +kubebuilder:scaffold:scheme
}

func main() {
	if err := run(); err != nil {
		setupLog.Error(err, "manager exited with an error")
		os.Exit(1)
	}
}

func run() error {
	var metricsAddr string
	var probeAddr string
	var enableLeaderElection bool
	opts := zap.Options{
		Development: true,
	}

	fs := flag.CommandLine
	fs.StringVar(&metricsAddr, "metrics-bind-address", ":8080",
		"The address the metrics endpoint binds to. Set to 0 to disable the metrics endpoint.")
	fs.StringVar(&probeAddr, "health-probe-bind-address", ":8081", "The address the probe endpoint binds to.")
	fs.BoolVar(&enableLeaderElection, "leader-elect", false,
		"Enable leader election for controller manager. "+
			"Enabling this will ensure there is only one active controller manager.")
	managerFlags := managerconfig.RegisterFlags(fs)
	opts.BindFlags(fs)
	flag.Parse()

	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&opts)))

	cfg, err := managerFlags.Resolve()
	if err != nil {
		return fmt.Errorf("invalid manager configuration: %w", err)
	}

	// The metrics endpoint is deliberately plain HTTP on a private bind
	// address rather than served through controller-runtime's
	// WithAuthenticationAndAuthorization filter: that filter requires the
	// manager's ServiceAccount to create TokenReviews and
	// SubjectAccessReviews, and no operator workload is permitted that
	// cluster-scoped grant. A NetworkPolicy restricts who can reach this
	// port; OpenShift-native TLS and ServiceMonitor scraping are added once
	// the chart exists to carry the certificate material.
	metricsServerOptions := metricsserver.Options{
		BindAddress:   metricsAddr,
		SecureServing: false,
	}

	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme:                 scheme,
		Metrics:                metricsServerOptions,
		HealthProbeBindAddress: probeAddr,
		LeaderElection:         enableLeaderElection,
		LeaderElectionID:       "marimohub-operator-leader.marimohub.io",
		Cache: cache.Options{
			// Restrict the manager's informer cache -- and therefore every
			// List/Watch it can serve -- to the single configured Runtime
			// namespace. This is the enforcement point for the namespace
			// trust boundary: the operator must never be able to observe or
			// act on a MarimoSession, Pod, Service, or Secret outside the
			// namespace untrusted notebook workloads run in.
			DefaultNamespaces: map[string]cache.Config{
				cfg.RuntimeNamespace: {},
			},
		},
	})
	if err != nil {
		return fmt.Errorf("failed to start manager: %w", err)
	}

	metrics := controller.NewMetrics()
	metrics.MustRegister()

	if err := (&controller.MarimoSessionReconciler{
		Client:    mgr.GetClient(),
		APIReader: mgr.GetAPIReader(),
		Scheme:    mgr.GetScheme(),
		Recorder:  mgr.GetEventRecorderFor("marimohub-operator"),
		Metrics:   metrics,
		Clock:     realclock.RealClock{},
		Backoff:   controller.DefaultBackoffPolicy,
		Options: session.Options{
			FetcherImage:       cfg.FetcherImage,
			InternalAPIURL:     cfg.InternalAPIURL,
			InternalAPICA:      cfg.InternalAPICA,
			IdleTimeout:        cfg.IdleTimeout,
			Resources:          cfg.DefaultResources,
			FetcherResources:   cfg.FetcherResources,
			ImagePullPolicy:    cfg.ImagePullPolicy,
			ServiceAccountName: cfg.ServiceAccountName,
			ImagePullSecrets:   cfg.ImagePullSecrets,
		},
		ImagePullDeadline: controller.DefaultImagePullDeadline,
		UnhealthyTimeout:  controller.DefaultUnhealthyTimeout,
		NodeLossDeadline:  controller.DefaultNodeLossDeadline,
		IdleGracePeriod:   controller.DefaultIdleGracePeriod,
	}).SetupWithManager(mgr); err != nil {
		return fmt.Errorf("failed to create controller %q: %w", "marimosession", err)
	}
	// +kubebuilder:scaffold:builder

	if err := mgr.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		return fmt.Errorf("failed to set up health check: %w", err)
	}
	if err := mgr.AddReadyzCheck("readyz", healthz.Ping); err != nil {
		return fmt.Errorf("failed to set up ready check: %w", err)
	}

	setupLog.Info("starting manager",
		"runtimeNamespace", cfg.RuntimeNamespace,
		"fetcherImage", cfg.FetcherImage,
		"internalAPIURL", cfg.InternalAPIURL,
	)
	if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
		return fmt.Errorf("manager exited: %w", err)
	}
	return nil
}
