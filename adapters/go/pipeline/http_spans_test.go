package pipeline

import (
	"context"
	"testing"
	"time"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

type httpRecordSampler struct{}

func (httpRecordSampler) ShouldSample(sdktrace.SamplingParameters) sdktrace.SamplingResult {
	return sdktrace.SamplingResult{Decision: sdktrace.RecordOnly}
}
func (httpRecordSampler) Description() string { return "HTTP record only" }

func TestUnclosedHTTPRecordingSpanRemainsCensoredAfterSDKShutdown(t *testing.T) {
	p, err := NewTraces(&traceSink{}, 16, 4, 0, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	provider := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(p))
	_, other := provider.Tracer("application").Start(context.Background(), "not HTTP")
	other.End()
	_, completed := provider.Tracer(httpScope).Start(context.Background(), "closed body")
	_, pending := provider.Tracer(httpScope).Start(context.Background(), "unclosed body")
	completed.End()
	if h := p.HTTPHealth(); h.Started != 2 || h.Ended != 1 || h.Active != 1 || h.Closed {
		t.Fatal("HTTP body evidence differs", h)
	}
	if err := provider.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	if h := p.HTTPHealth(); h.Active != 1 || !h.Closed {
		t.Fatal("shutdown invented HTTP completion", h)
	}
	pending.End()
	// The SDK stops notifying processors at shutdown. Its unobserved late
	// End must not replace the retained incomplete checkpoint with success.
	if h := p.HTTPHealth(); h.Started != 2 || h.Ended != 1 || h.Active != 1 {
		t.Fatal("late end invented observed SDK completion", h)
	}
}

func TestRecordOnlyHTTPSpansCountWithoutBeingExported(t *testing.T) {
	sink := &traceSink{}
	p, err := NewTraces(sink, 8, 2, 0, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	provider := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(p), sdktrace.WithSampler(httpRecordSampler{}))
	_, span := provider.Tracer(httpScope).Start(context.Background(), "record only")
	span.End()
	if err := provider.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	if h := p.HTTPHealth(); h.Started != 1 || h.Ended != 1 || h.Active != 0 {
		t.Fatal("record-only lifecycle lost", h)
	}
	if p.Health().Exported != 0 {
		t.Fatal("record-only span exported")
	}
}
