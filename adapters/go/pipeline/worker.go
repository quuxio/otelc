// Package pipeline owns export scheduling for otelc's isolated SDK providers.
// It never installs global providers or invokes the application's error handler.
package pipeline

import (
	"context"
	"sync/atomic"
	"time"
)

// Health counts exporter API outcomes, not independent downstream receipt.
type Health struct {
	Attempts, Succeeded, Failed uint64
	Exported, Dropped, Pending  uint64
	Closed                      bool
}

type counters struct {
	attempts, succeeded, failed atomic.Uint64
	exported, dropped           atomic.Uint64
}

func (c *counters) snapshot(pending int, closed bool) Health {
	return Health{Attempts: c.attempts.Load(), Succeeded: c.succeeded.Load(),
		Failed: c.failed.Load(), Exported: c.exported.Load(), Dropped: c.dropped.Load(),
		Pending: uint64(pending), Closed: closed}
}

// acquire does not admit a new export after cancellation, even when the token
// and context become ready together.
func acquire(ctx context.Context, token chan struct{}) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-token:
		if err := ctx.Err(); err != nil {
			token <- struct{}{}
			return err
		}
		return nil
	}
}

type worker struct {
	cancel context.CancelFunc
	done   chan struct{}
}

func start(interval, timeout time.Duration, flush func(context.Context) error) worker {
	ctx, cancel := context.WithCancel(context.Background())
	w := worker{cancel: cancel, done: make(chan struct{})}
	if interval == 0 {
		close(w.done)
		return w
	}
	go func() {
		defer close(w.done)
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				phase, stop := context.WithTimeout(ctx, timeout)
				// Flush records failures in this pipeline's health. It deliberately
				// does not route them through the application's global otel.Handle.
				_ = flush(phase)
				stop()
			}
		}
	}()
	return w
}
