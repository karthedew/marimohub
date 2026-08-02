package controller

import (
	"testing"
	"time"
)

func TestBackoffPolicyDurationEscalatesAndCaps(t *testing.T) {
	b := BackoffPolicy{Base: time.Second, Max: 10 * time.Second, Jitter: 0}
	cases := []struct {
		attempt int32
		want    time.Duration
	}{
		{0, time.Second}, // clamped up to attempt 1
		{1, time.Second},
		{2, 2 * time.Second},
		{3, 4 * time.Second},
		{4, 8 * time.Second},
		{5, 10 * time.Second}, // would be 16s uncapped; Max wins
		{50, 10 * time.Second},
	}
	for _, c := range cases {
		if got := b.Duration(c.attempt); got != c.want {
			t.Errorf("Duration(%d) = %v, want %v", c.attempt, got, c.want)
		}
	}
}

func TestBackoffPolicyDurationAppliesJitter(t *testing.T) {
	b := BackoffPolicy{
		Base: 10 * time.Second, Max: time.Minute, Jitter: 0.5,
		Rand: func() float64 { return 0 }, // factor = 1 - jitter = 0.5
	}
	if got, want := b.Duration(1), 5*time.Second; got != want {
		t.Errorf("Duration(1) with Rand()=0 = %v, want %v", got, want)
	}

	b.Rand = func() float64 { return 1 } // factor = 1 + jitter = 1.5
	if got, want := b.Duration(1), 15*time.Second; got != want {
		t.Errorf("Duration(1) with Rand()=1 = %v, want %v", got, want)
	}
}

func TestBackoffPolicyDurationJitterAppliesAfterMaxClamp(t *testing.T) {
	b := BackoffPolicy{
		Base: time.Minute, Max: time.Minute, Jitter: 0.5,
		Rand: func() float64 { return 1 },
	}
	// Jitter is applied after the Max clamp, so a fully-escalated delay with
	// maximal positive jitter is expected to exceed Max: Max only bounds the
	// unjittered exponential term, not the final jittered result. This test
	// pins that documented behavior rather than silently assuming otherwise.
	got := b.Duration(10)
	if got <= b.Max {
		t.Errorf("Duration(10) = %v, want greater than Max %v under maximal positive jitter", got, b.Max)
	}
}
