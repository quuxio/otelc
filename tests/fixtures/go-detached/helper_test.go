package trace

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"

	"example.com/otelc-detach-fixture/bridge"
	"example.com/otelc-detach-fixture/gls"
	sdk "go.opentelemetry.io/otel/sdk/trace"
	api "go.opentelemetry.io/otel/trace"
)

// These are the exact private field shapes used by the pinned upstream bridge.
// The generated helper body is compiled unchanged against these shapes and real
// SDK spans. Separate compiler tests check actual TLS, hooks and task inheritance.
type spanWrapper struct {
	span  api.Span
	prev  *spanWrapper
	ended *atomic.Bool
}
type traceContext struct {
	sw  *spanWrapper
	n   int
	lcs api.Span
}
type spanKey struct {
	traceID api.TraceID
	spanID  api.SpanID
}

var spanStates = struct {
	sync.Mutex
	states map[spanKey]*atomic.Bool
}{states: make(map[spanKey]*atomic.Bool)}

func TestDetachKeepsLiveSpanAndRemovesOnlyItsFrameAndPropagationState(t *testing.T) {
	provider := sdk.NewTracerProvider(sdk.WithoutPanicRecording())
	defer func() {
		if err := provider.Shutdown(context.Background()); err != nil {
			t.Error(err)
		}
	}()
	span := func(name string) api.Span {
		_, value := provider.Tracer("fixture").Start(context.Background(), name)
		t.Cleanup(func() { value.End() })
		return value
	}
	a, b, c, absent := span("root"), span("middle"), span("head"), span("absent")
	if bridge.Detach == nil {
		t.Fatal("generated init did not register the helper")
	}
	if bridge.Detach(nil) || bridge.Detach(api.SpanFromContext(context.Background())) {
		t.Fatal("accepted nil or invalid context")
	}
	runtime.Current = nil
	if bridge.Detach(absent) {
		t.Fatal("accepted a span without a current TLS context")
	}
	tc := &traceContext{}
	runtime.Current = tc
	for _, value := range []api.Span{a, b, c} {
		state := &atomic.Bool{}
		tc.sw = &spanWrapper{span: value, prev: tc.sw, ended: state}
		tc.n++
		sc := value.SpanContext()
		spanStates.states[spanKey{sc.TraceID(), sc.SpanID()}] = state
	}
	tc.lcs = a
	if bridge.Detach(absent) || tc.n != 3 || len(spanStates.states) != 3 {
		t.Fatal("altered unrelated context")
	}
	// Remove a middle frame, then the root, then the only remaining head.
	for _, step := range []struct {
		value api.Span
		root  api.Span
		left  int
	}{{b, a, 2}, {a, c, 1}, {c, nil, 0}} {
		assertDetachedFrame(t, tc, step.value, step.root, step.left)
	}
	if tc.sw != nil {
		t.Fatal("retained an empty stack")
	}
	runtime.Current = nil
}

func assertDetachedFrame(t *testing.T, tc *traceContext, value, root api.Span, left int) {
	t.Helper()
	var state *atomic.Bool
	for frame := tc.sw; frame != nil; frame = frame.prev {
		if frame.span == value {
			state = frame.ended
		}
	}
	if state == nil || !bridge.Detach(value) || !value.IsRecording() || state.Load() {
		t.Fatal("detachment ended or lost the live span")
	}
	if tc.n != left || tc.lcs != root || len(spanStates.states) != left {
		t.Fatal("unbalanced frame, local root or retained propagation state", tc.n, len(spanStates.states))
	}
	if bridge.Detach(value) {
		t.Fatal("detached twice")
	}
}

func TestDetachPreservesTheBoundaryOfAnInheritedContext(t *testing.T) {
	provider := sdk.NewTracerProvider(sdk.WithoutPanicRecording())
	defer func() {
		if err := provider.Shutdown(context.Background()); err != nil {
			t.Error(err)
		}
	}()
	_, life := provider.Tracer("fixture").Start(context.Background(), "lifetime")
	defer life.End()
	inherited := api.SpanFromContext(api.ContextWithSpanContext(context.Background(),
		api.NewSpanContext(api.SpanContextConfig{TraceID: api.TraceID{1}, SpanID: api.SpanID{1}})))
	key := spanKey{life.SpanContext().TraceID(), life.SpanContext().SpanID()}
	spanStates.states[key] = &atomic.Bool{}
	defer delete(spanStates.states, key)
	// Upstream Clone captures an inherited parent with no local-root span.
	tc := &traceContext{n: 2, sw: &spanWrapper{span: life, ended: &atomic.Bool{},
		prev: &spanWrapper{span: inherited, ended: &atomic.Bool{}}}}
	runtime.Current = tc
	defer func() { runtime.Current = nil }()
	if !bridge.Detach(life) || tc.n != 1 || tc.sw.span.SpanContext().SpanID() != inherited.SpanContext().SpanID() || tc.lcs != nil {
		t.Fatal("detachment turned an inherited parent into a local-root span")
	}
	if !life.IsRecording() {
		t.Fatal("detachment ended the lifetime")
	}
}
