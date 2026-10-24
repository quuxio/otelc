package probe

import (
	"runtime"
	"sync/atomic"
)

// Completion observes only whether an auxiliary constructor/release returns
// normally. It creates no function span, instrument or measurement.
type Completion struct {
	owner    *Recorder
	boundary string
	ended    atomic.Bool
}

// Watch must be called directly in the observed function. Lifetime selection
// does not implicitly opt that function into the independent function selection.
func (p *Recorder) Watch() *Completion {
	p.mu.Lock()
	if p.closed {
		p.mu.Unlock()
		return nil
	}
	if p.active.Load()+p.observerActive.Load() >= uint64(p.options.MaxActive) {
		p.observerLoss.Add(1)
		p.mu.Unlock()
		return nil
	}
	p.observerActive.Add(1)
	p.observerStarts.Add(1)
	p.mu.Unlock()
	boundary := ""
	if pc, _, _, ok := runtime.Caller(1); ok {
		if function := runtime.FuncForPC(pc); function != nil {
			boundary = function.Name()
		}
	}
	return &Completion{owner: p, boundary: boundary}
}

// End returns true only for known normal completion with a nil error interface.
// Generated hooks use that acknowledgement before admitting an object/release.
func (c *Completion) End(returnedError bool) bool {
	if c == nil || c.ended.Swap(true) {
		return false
	}
	kind := unwind(c.boundary)
	if kind == "unknown" {
		c.owner.unknown.Add(1)
	}
	c.owner.observerEnds.Add(1)
	c.owner.observerActive.Add(^uint64(0))
	return kind == "normal" && !returnedError
}
