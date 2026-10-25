package trace

import (
	"context"
	api "go.opentelemetry.io/otel/trace"
	"sync/atomic"
)

type spanWrapper struct {
	span      api.Span
	prev      *spanWrapper
	ended     *atomic.Bool
	telemetry context.Context
}

type traceContext struct {
	sw  *spanWrapper
	n   int
	lcs api.Span
}

// This is the exact current-goroutine tail algorithm from the pinned bridge.
func (tc *traceContext) tail() api.Span {
	for tc.sw != nil && tc.sw.ended.Load() {
		tc.sw = tc.sw.prev
		tc.n--
	}
	if tc.sw == nil {
		tc.lcs = nil
		return nil
	}
	return tc.sw.span
}
