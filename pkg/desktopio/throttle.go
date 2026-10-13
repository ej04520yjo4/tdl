package desktopio

import (
	"sync"
	"time"
)

// ProgressInterval is the minimum spacing between two transfer progress
// events for one item. The transfer layers report every written part (up to a
// few dozen times per second and item); the desktop client persists and
// renders each event, so an unbounded stream slows the transfer itself.
const ProgressInterval = 250 * time.Millisecond

// Throttle rate-limits per-item progress events and derives a transfer rate
// from the samples it lets through. State changes (queued, completed, failed)
// are not progress samples and must be emitted without consulting it.
type Throttle struct {
	interval time.Duration
	now      func() time.Time

	mu   sync.Mutex
	last map[string]throttleSample
}

type throttleSample struct {
	at    time.Time
	value int64
}

func NewThrottle(interval time.Duration) *Throttle {
	return &Throttle{
		interval: interval,
		now:      time.Now,
		last:     make(map[string]throttleSample),
	}
}

// Allow reports whether a progress event for key may be emitted now. When it
// may, rate is the average bytes per second since the previous emitted sample,
// or zero while that is still unknown.
func (t *Throttle) Allow(key string, value int64) (rate int64, ok bool) {
	now := t.now()

	t.mu.Lock()
	defer t.mu.Unlock()

	last, seen := t.last[key]
	if seen && now.Sub(last.at) < t.interval {
		return 0, false
	}
	t.last[key] = throttleSample{at: now, value: value}
	if !seen {
		return 0, true
	}

	elapsed := now.Sub(last.at).Seconds()
	delta := value - last.value
	if elapsed <= 0 || delta <= 0 {
		return 0, true
	}
	return int64(float64(delta) / elapsed), true
}

// Forget drops the sample of a finished item so long jobs stay bounded.
func (t *Throttle) Forget(key string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	delete(t.last, key)
}
