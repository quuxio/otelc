package pipeline

import (
	"context"
	"errors"
	"sync"
	"time"

	sdk "go.opentelemetry.io/otel/sdk/trace"
)

// Traces is an owned SDK span processor with bounded non-blocking admission.
// Failed batches remain queued; SDK span construction, sampling and context are
// unchanged. It imposes no complete-root-tree requirement on delayed children.
type Traces struct {
	exporter sdk.SpanExporter
	capacity int
	batch    int
	mu       sync.Mutex
	queue    []sdk.ReadOnlySpan
	closed   bool
	closeErr error
	token    chan struct{}
	worker   worker
	health   counters
}

// NewTraces owns the supplied exporter. A zero interval uses explicit flushes.
func NewTraces(exporter sdk.SpanExporter, capacity, batch int, interval, timeout time.Duration) (*Traces, error) {
	if exporter == nil || capacity <= 0 || batch <= 0 || batch > capacity || interval < 0 || timeout <= 0 {
		return nil, errors.New("invalid owned trace pipeline settings")
	}
	p := &Traces{exporter: exporter, capacity: capacity, batch: batch, token: make(chan struct{}, 1)}
	p.token <- struct{}{}
	p.worker = start(interval, timeout, p.ForceFlush)
	return p, nil
}

// OnStart intentionally leaves SDK and application context unchanged.
func (p *Traces) OnStart(context.Context, sdk.ReadWriteSpan) {}

// OnEnd never calls application code or performs network IO.
func (p *Traces) OnEnd(span sdk.ReadOnlySpan) {
	// Record-only sampling exposes the span to processors without permitting
	// export. Match the official SDK's simple and batch processors.
	if !span.SpanContext().IsSampled() {
		return
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.closed || len(p.queue) == p.capacity {
		p.health.dropped.Add(1)
		return
	}
	p.queue = append(p.queue, span)
}

func (p *Traces) drain(ctx context.Context) error {
	p.mu.Lock()
	remaining := len(p.queue)
	p.mu.Unlock()
	for remaining > 0 {
		count := min(p.batch, remaining)
		p.mu.Lock()
		batch := append([]sdk.ReadOnlySpan(nil), p.queue[:count]...)
		p.mu.Unlock()
		p.health.attempts.Add(1)
		if err := p.exporter.ExportSpans(ctx, batch); err != nil {
			p.health.failed.Add(1)
			return err
		}
		p.health.succeeded.Add(1)
		p.health.exported.Add(uint64(count))
		p.mu.Lock()
		// Clear references to exported spans, including their resource/attribute
		// storage. Concurrent OnEnd only appends; drain owns the export token.
		clear(p.queue[:count])
		p.queue = p.queue[count:]
		p.mu.Unlock()
		remaining -= count
	}
	return nil
}

// ForceFlush exports the spans admitted before this drain, leaving failed
// batches visible as pending. Concurrent producers cannot make it run forever.
func (p *Traces) ForceFlush(ctx context.Context) error {
	if err := acquire(ctx, p.token); err != nil {
		return err
	}
	defer func() { p.token <- struct{}{} }()
	p.mu.Lock()
	closed := p.closed
	p.mu.Unlock()
	if closed {
		return errors.New("owned trace pipeline is shut down")
	}
	return p.drain(ctx)
}

// Health keeps API success separate from downstream receipt and pending loss.
func (p *Traces) Health() Health {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.health.snapshot(len(p.queue), p.closed)
}

// Shutdown stops admission and periodic exports, then attempts bounded final
// drain and exporter shutdown. Remaining spans stay visible in Health.
func (p *Traces) Shutdown(ctx context.Context) error {
	p.worker.cancel()
	if err := acquire(ctx, p.token); err != nil {
		return err
	}
	defer func() { p.token <- struct{}{} }()
	p.mu.Lock()
	if p.closed {
		err := p.closeErr
		p.mu.Unlock()
		return err
	}
	p.closed = true
	p.mu.Unlock()
	err := errors.Join(p.drain(ctx), p.exporter.Shutdown(ctx))
	p.mu.Lock()
	p.closeErr = err
	p.mu.Unlock()
	return err
}
