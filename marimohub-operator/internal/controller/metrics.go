package controller

import (
	"time"

	"github.com/prometheus/client_golang/prometheus"
	ctrlmetrics "sigs.k8s.io/controller-runtime/pkg/metrics"

	"github.com/karthedew/marimohub/marimohub-operator/api/v1alpha1"
)

// Metrics holds the Prometheus collectors the reconciler updates as it
// observes state transitions. Every label below is drawn from a small,
// fixed enumeration -- lifecycle phase, Runtime mode, or condition Reason --
// never a Runtime, Notebook, User, or Workspace identifier. Labeling by any
// of those would turn each metric's cardinality into a function of how many
// Runtimes have ever existed, which defeats the point of a phase/attempt
// counter and is exactly the kind of label this operator must never emit.
type Metrics struct {
	phaseCount       *prometheus.GaugeVec
	startupDuration  *prometheus.HistogramVec
	idleShutdowns    *prometheus.CounterVec
	attempts         *prometheus.CounterVec
	capacityRejected *prometheus.CounterVec
	failuresByReason *prometheus.CounterVec
	reconcileErrors  prometheus.Counter
}

// modeLabel is the Runtime-mode (edit/run/deploy) label shared by every
// per-mode collector below: a small, fixed enumeration, unlike the identity
// labels this package's own doc comment forbids.
const modeLabel = "mode"

// NewMetrics constructs a fresh, unregistered Metrics. Tests can use one
// without ever calling MustRegister, since a Metrics value only ever talks
// to its own collectors, never the global registry, until registered.
func NewMetrics() *Metrics {
	return &Metrics{
		phaseCount: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "marimohub_runtime_phase_count",
			Help: "Current number of MarimoSession Runtimes observed in each lifecycle phase.",
		}, []string{"phase"}),
		startupDuration: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name:    "marimohub_runtime_startup_duration_seconds",
			Help:    "Time from a Runtime entering Starting to its Pod passing authenticated readiness.",
			Buckets: prometheus.DefBuckets,
		}, []string{modeLabel}),
		idleShutdowns: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "marimohub_runtime_idle_shutdowns_total",
			Help: "Idle shutdown actions taken: CR deletion for edit/run, Pod sleep for deploy.",
		}, []string{modeLabel}),
		attempts: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "marimohub_runtime_start_attempts_total",
			Help: "Pod creation attempts, including retries after quota rejection or infrastructure loss.",
		}, []string{modeLabel}),
		capacityRejected: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "marimohub_runtime_capacity_rejections_total",
			Help: "Pod creation attempts confirmed rejected by a namespace ResourceQuota.",
		}, []string{modeLabel}),
		failuresByReason: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "marimohub_runtime_failures_total",
			Help: "Runtimes that entered the Failed phase, by condition Reason.",
		}, []string{"reason"}),
		reconcileErrors: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "marimohub_runtime_reconcile_errors_total",
			Help: "Reconcile invocations that returned an error.",
		}),
	}
}

// MustRegister adds every collector to controller-runtime's shared registry,
// which the manager already exposes on its metrics endpoint; callers that
// only need Metrics for its own sake (unit tests, envtest) can skip this
// entirely and never touch the global registry.
func (m *Metrics) MustRegister() {
	ctrlmetrics.Registry.MustRegister(
		m.phaseCount,
		m.startupDuration,
		m.idleShutdowns,
		m.attempts,
		m.capacityRejected,
		m.failuresByReason,
		m.reconcileErrors,
	)
}

func (m *Metrics) observePhaseChange(from, to v1alpha1.RuntimePhase) {
	if m == nil {
		return
	}
	if from != "" {
		m.phaseCount.WithLabelValues(string(from)).Dec()
	}
	if to != "" {
		m.phaseCount.WithLabelValues(string(to)).Inc()
	}
}

func (m *Metrics) observeStartupDuration(mode v1alpha1.RuntimeMode, d time.Duration) {
	if m == nil {
		return
	}
	m.startupDuration.WithLabelValues(string(mode)).Observe(d.Seconds())
}

func (m *Metrics) observeIdleShutdown(mode v1alpha1.RuntimeMode) {
	if m == nil {
		return
	}
	m.idleShutdowns.WithLabelValues(string(mode)).Inc()
}

func (m *Metrics) observeAttempt(mode v1alpha1.RuntimeMode) {
	if m == nil {
		return
	}
	m.attempts.WithLabelValues(string(mode)).Inc()
}

func (m *Metrics) observeCapacityRejection(mode v1alpha1.RuntimeMode) {
	if m == nil {
		return
	}
	m.capacityRejected.WithLabelValues(string(mode)).Inc()
}

func (m *Metrics) observeFailure(reason string) {
	if m == nil {
		return
	}
	m.failuresByReason.WithLabelValues(reason).Inc()
}

func (m *Metrics) observeReconcileError() {
	if m == nil {
		return
	}
	m.reconcileErrors.Inc()
}
