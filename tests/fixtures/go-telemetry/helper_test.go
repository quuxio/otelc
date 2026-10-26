package trace

import (
	"context"
	"sync/atomic"
	"testing"
	"time"

	"go.opentelemetry.io/otel/baggage"
	"go.opentelemetry.io/otel/otelc-telemetry-fixture/bridge"
	runtime "go.opentelemetry.io/otel/otelc-telemetry-fixture/gls"
	api "go.opentelemetry.io/otel/trace"
)

func span(id byte) api.Span {
	sc := api.NewSpanContext(api.SpanContextConfig{TraceID: api.TraceID{1}, SpanID: api.SpanID{id}, TraceFlags: api.FlagsSampled})
	return api.SpanFromContext(api.ContextWithSpanContext(context.Background(), sc))
}

func withBaggage(ctx context.Context, value string) context.Context {
	b, err := baggage.Parse(value)
	if err != nil {
		panic(err)
	}
	return baggage.ContextWithBaggage(ctx, b)
}

func TestTelemetrySnapshotsOverrideClearAndDoNotRetainApplicationContext(t *testing.T) {
	parent, child := span(2), span(3)
	background := context.Background()
	runtime.Current = nil
	otelcCaptureTelemetry(parent, background)
	if bridge.ContextWithImplicitTelemetry(background) != background {
		t.Fatal("invented an absent parent")
	}
	tc := &traceContext{}
	runtime.Current = tc
	otelcCaptureTelemetry(nil, background)
	otelcCaptureTelemetry(parent, background)
	if bridge.ContextWithImplicitTelemetry(background) != background {
		t.Fatal("invented an empty-stack parent")
	}
	tc.sw = &spanWrapper{span: parent, ended: &atomic.Bool{}}
	tc.n = 1
	// A rejected/overflowed frame must not retag the previous frame.
	otelcCaptureTelemetry(child, withBaggage(background, "wrong=frame"))
	if tc.sw.telemetry != nil {
		t.Fatal("retagged a different span")
	}
	if baggage.FromContext(bridge.ContextWithImplicitTelemetry(background)).Len() != 0 {
		t.Fatal("invented baggage before capture")
	}
	assertApplicationSnapshot(t, tc, parent, background)
	value := bridge.ContextWithImplicitTelemetry(background)
	if !api.SpanContextFromContext(value).Equal(parent.SpanContext()) || baggage.FromContext(value).Member("tenant").Value() != "parent" {
		t.Fatal("lost the matching telemetry snapshot")
	}
	// Clearing works even without an explicit span context.
	for _, explicit := range []context.Context{baggage.ContextWithoutBaggage(background), withBaggage(background, "tenant=caller")} {
		value = bridge.ContextWithImplicitTelemetry(explicit)
		if baggage.FromContext(value).String() != baggage.FromContext(explicit).String() {
			t.Fatal("overrode explicit baggage or clearing")
		}
	}
	explicit, stop := context.WithCancel(withBaggage(api.ContextWithSpan(background, child), "tenant=caller"))
	stop()
	if value := bridge.ContextWithImplicitTelemetry(explicit); value != explicit || value.Err() != context.Canceled {
		t.Fatal("overrode explicit span, baggage or cancellation")
	}
	// Ended heads restore the previous frame's own baggage, never a global slot.
	previous := tc.sw
	tc.sw = &spanWrapper{span: child, prev: previous, ended: &atomic.Bool{}}
	tc.n++
	otelcCaptureTelemetry(child, withBaggage(background, "tenant=child"))
	if baggage.FromContext(bridge.ContextWithImplicitTelemetry(background)).Member("tenant").Value() != "child" {
		t.Fatal("child did not get its own snapshot")
	}
	tc.sw.ended.Store(true)
	if baggage.FromContext(bridge.ContextWithImplicitTelemetry(background)).Member("tenant").Value() != "parent" {
		t.Fatal("ended child leaked its baggage")
	}
	previous.ended.Store(true)
	if bridge.ContextWithImplicitTelemetry(background) != background {
		t.Fatal("ended parent leaked telemetry")
	}
}

func assertApplicationSnapshot(t *testing.T, tc *traceContext, parent api.Span, background context.Context) {
	t.Helper()
	type key struct{}
	application, cancel := context.WithDeadline(context.WithValue(background, key{}, "application"), time.Now().Add(time.Hour))
	application = withBaggage(application, "tenant=parent,task=one")
	otelcCaptureTelemetry(parent, application)
	cancel()
	snapshot := tc.sw.telemetry
	if snapshot.Err() != nil || snapshot.Done() != nil || snapshot.Value(key{}) != nil {
		t.Fatal("retained application cancellation or values")
	}
	if _, ok := snapshot.Deadline(); ok {
		t.Fatal("retained an application deadline")
	}
}
