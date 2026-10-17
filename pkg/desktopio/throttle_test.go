package desktopio

import (
	"testing"
	"time"
)

func TestThrottleLimitsPerKeyAndReportsRate(t *testing.T) {
	clock := time.Unix(1000, 0)
	throttle := NewThrottle(250 * time.Millisecond)
	throttle.now = func() time.Time { return clock }

	if rate, ok := throttle.Allow("a", 0); !ok || rate != 0 {
		t.Fatalf("first sample = (%d, %v), want (0, true)", rate, ok)
	}
	// Another item is independent of the first one.
	if _, ok := throttle.Allow("b", 0); !ok {
		t.Fatal("first sample of another key was throttled")
	}

	clock = clock.Add(100 * time.Millisecond)
	if _, ok := throttle.Allow("a", 1<<20); ok {
		t.Fatal("sample inside the interval was not throttled")
	}

	// The rate covers the whole span since the last emitted sample, including
	// the bytes of the throttled one.
	clock = clock.Add(400 * time.Millisecond)
	rate, ok := throttle.Allow("a", 2<<20)
	if !ok {
		t.Fatal("sample after the interval was throttled")
	}
	if want := int64(4 << 20); rate != want {
		t.Fatalf("rate = %d, want %d", rate, want)
	}

	throttle.Forget("a")
	if rate, ok := throttle.Allow("a", 5); !ok || rate != 0 {
		t.Fatalf("sample after Forget = (%d, %v), want (0, true)", rate, ok)
	}
}
