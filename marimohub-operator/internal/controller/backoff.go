package controller

import (
	"math/rand"
	"time"
)

// BackoffPolicy computes how long the controller waits before recreating a
// Pod lost to infrastructure (deletion outside the controller, node loss,
// eviction, or preemption). It escalates with each consecutive attempt and
// adds jitter so a cluster-wide event (a node pool draining, for example)
// does not make every affected Runtime retry in lockstep.
type BackoffPolicy struct {
	// Base is the delay for the first retry.
	Base time.Duration
	// Max caps the delay regardless of how many attempts have accumulated.
	Max time.Duration
	// Jitter is a fraction in [0,1]; the returned delay is scaled by a
	// uniform random factor in [1-Jitter, 1+Jitter].
	Jitter float64
	// Rand returns a value in [0,1). Nil defaults to a package-level
	// math/rand source; tests that need a deterministic delay supply their
	// own so the exact jittered value is predictable.
	Rand func() float64
}

// DefaultBackoffPolicy is a reasonable production default: a five-second
// first retry, doubling up to a five-minute ceiling, with twenty percent
// jitter. It is a constant here rather than a manager flag because, unlike
// the per-mode idle timeouts, nothing in the chart's documented value shape
// anticipates configuring it yet; wiring it through is a mechanical change
// whenever that need arises.
var DefaultBackoffPolicy = BackoffPolicy{
	Base:   5 * time.Second,
	Max:    5 * time.Minute,
	Jitter: 0.2,
}

// Duration returns the delay to wait before the given attempt number (1 for
// the first retry after a loss, 2 for the second, and so on). attempt values
// less than 1 are treated as 1.
func (b BackoffPolicy) Duration(attempt int32) time.Duration {
	if attempt < 1 {
		attempt = 1
	}
	const maxShift = 20 // caps 1<<shift well below any realistic Base*2^n overflow
	shift := min(attempt-1, maxShift)
	d := b.Base * time.Duration(uint64(1)<<uint(shift))
	if d <= 0 || d > b.Max {
		d = b.Max
	}
	if b.Jitter <= 0 {
		return d
	}
	r := b.randFloat()
	factor := 1 - b.Jitter + r*2*b.Jitter
	return time.Duration(float64(d) * factor)
}

func (b BackoffPolicy) randFloat() float64 {
	if b.Rand != nil {
		return b.Rand()
	}
	return rand.Float64() //nolint:gosec // jitter has no security relevance
}
