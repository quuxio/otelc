package pipeline

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	api "go.opentelemetry.io/otel/metric"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	traceapi "go.opentelemetry.io/otel/trace"
	"io.quux.otelc/go/metriccontrol"
)

var rejected = errors.New("fixture export rejected")

type metricSink struct {
	mu       sync.Mutex
	values   []int64
	fail     bool
	started  chan struct{}
	release  chan struct{}
	closed   atomic.Int64
	callback func()
}

func (*metricSink) Temporality(sdkmetric.InstrumentKind) metricdata.Temporality {
	return metricdata.DeltaTemporality // The owned wrapper must override this.
}
func (*metricSink) Aggregation(k sdkmetric.InstrumentKind) sdkmetric.Aggregation {
	return sdkmetric.DefaultAggregationSelector(k)
}
func (s *metricSink) Export(ctx context.Context, data *metricdata.ResourceMetrics) error {
	if s.started != nil {
		select {
		case s.started <- struct{}{}:
		default:
		}
		select {
		case <-s.release:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	if s.callback != nil {
		s.callback()
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, scope := range data.ScopeMetrics {
		for _, metric := range scope.Metrics {
			if sum, ok := metric.Data.(metricdata.Sum[int64]); ok {
				if sum.Temporality != metricdata.CumulativeTemporality {
					return errors.New("lost cumulative temporality")
				}
				for _, point := range sum.DataPoints {
					s.values = append(s.values, point.Value)
				}
			}
		}
	}
	if s.fail {
		return rejected
	}
	return nil
}
func (*metricSink) ForceFlush(context.Context) error { return nil }
func (s *metricSink) Shutdown(context.Context) error {
	s.closed.Add(1)
	return nil
}

func check(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

func TestMetricCollectionOffSuppressesCachedSnapshotsAndFinalDrain(t *testing.T) {
	ctx := context.Background()
	controls := metriccontrol.New(true, true)
	sink := &metricSink{}
	p, err := NewMetrics(controls, sink, resource.Empty(), 0, time.Second)
	check(t, err)
	meter := p.Provider.Meter(Scope)
	counter, err := meter.Int64Counter("calls")
	check(t, err)
	counter.Add(ctx, 7)
	check(t, p.Flush(ctx))
	before := p.Health().Attempts
	controls.SetCollection(false)
	check(t, p.Flush(ctx))
	if p.Health().Attempts != before {
		t.Fatal("collection-off exported a stale snapshot")
	}
	controls.SetCollection(true)
	_, err = meter.Int64ObservableCounter("disable-during-collection", api.WithInt64Callback(func(ctx context.Context, result api.Int64Observer) error {
		controls.SetCollection(false)
		result.Observe(99)
		return nil
	}))
	check(t, err)
	check(t, p.Flush(ctx))
	if p.Health().Attempts != before {
		t.Fatal("disabled callback exported prior cached data")
	}
	check(t, p.Shutdown(ctx))
	if p.Health().Attempts != before || sink.closed.Load() != 1 {
		t.Fatal("shutdown bypassed collection-off")
	}
}

func TestMetricsRetainMeasurementsAcrossEveryInitialStateAndRetryFailure(t *testing.T) {
	ctx := context.Background()
	for _, collection := range []bool{false, true} {
		for _, emission := range []bool{false, true} {
			t.Run(fmt.Sprint(collection, "/", emission), func(t *testing.T) {
				controls := metriccontrol.New(collection, emission)
				sink := &metricSink{fail: true}
				p, err := NewMetrics(controls, sink, resource.Empty(), 0, time.Second)
				check(t, err)
				counter, err := p.Provider.Meter(Scope).Int64Counter("calls")
				check(t, err)
				counter.Add(ctx, 5)
				err = p.Flush(ctx)
				if (emission && collection) != errors.Is(err, rejected) {
					t.Fatal("export error was suppressed", err)
				}
				controls.SetCollection(true)
				check(t, controls.SetEmission(ctx, false))
				counter.Add(ctx, 7)
				check(t, p.Flush(ctx))
				held := p.Health().Attempts
				check(t, controls.SetEmission(ctx, true))
				sink.fail = false
				check(t, p.Flush(ctx))
				controls.SetCollection(false)
				counter.Add(ctx, 100)
				beforeOff := p.Health().Attempts
				check(t, p.Flush(ctx))
				if p.Health().Attempts != beforeOff {
					t.Fatal("disabled collection exported old snapshots")
				}
				controls.SetCollection(true)
				check(t, p.Flush(ctx))
				want := int64(7)
				if collection {
					want += 5
				}
				if len(sink.values) < 2 || sink.values[len(sink.values)-1] != want || sink.values[len(sink.values)-2] != want {
					t.Fatal("lost paused measurements or admitted disabled measurement", sink.values)
				}
				if h := p.Health(); h.Attempts != held+2 || h.Succeeded != 2 || h.Failed != held {
					t.Fatal(h)
				}
				// Other scopes cannot enter this owned provider.
				foreign, err := p.Provider.Meter("application").Int64Counter("foreign")
				check(t, err)
				foreign.Add(ctx, 99)
				check(t, p.Shutdown(ctx))
				check(t, p.Shutdown(ctx))
				if sink.closed.Load() != 1 || !p.Health().Closed || !errors.Is(p.Flush(ctx), sdkmetric.ErrExporterShutdown) {
					t.Fatal("shutdown contract", p.Health())
				}
			})
		}
	}
}

func TestMetricExportBarrierAndCancelledShutdownCanBeRetried(t *testing.T) {
	sink := &metricSink{started: make(chan struct{}, 1), release: make(chan struct{})}
	controls := metriccontrol.New(true, true)
	p, err := NewMetrics(controls, sink, resource.Empty(), 0, time.Second)
	check(t, err)
	result := make(chan error, 1)
	go func() { result <- p.Flush(context.Background()) }()
	<-sink.started
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer cancel()
	if err := controls.SetEmission(ctx, false); !errors.Is(err, context.DeadlineExceeded) || !controls.EmissionEnabled() {
		t.Fatal("falsely acknowledged emission barrier", err)
	}
	if err := p.Shutdown(ctx); !errors.Is(err, context.DeadlineExceeded) || p.Health().Closed {
		t.Fatal("cancelled admission closed the pipeline", err)
	}
	close(sink.release)
	check(t, <-result)
	// Cancellation during the final export must not permanently prevent cleanup.
	phase, stop := context.WithCancel(context.Background())
	sink.callback = stop
	if err := p.Shutdown(phase); !errors.Is(err, context.Canceled) || sink.closed.Load() != 0 {
		t.Fatal("expected retryable exporter shutdown", err, sink.closed.Load())
	}
	if err := p.Shutdown(context.Background()); !errors.Is(err, context.Canceled) || sink.closed.Load() != 1 {
		t.Fatal("cleanup retry lost previous drain failure or did not close", err, sink.closed.Load())
	}
}

type traceSink struct {
	mu      sync.Mutex
	names   []string
	fail    bool
	closed  atomic.Int64
	started chan struct{}
	release chan struct{}
}

func (s *traceSink) ExportSpans(ctx context.Context, spans []sdktrace.ReadOnlySpan) error {
	if s.started != nil {
		select {
		case s.started <- struct{}{}:
		default:
		}
		select {
		case <-s.release:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.fail {
		return rejected
	}
	for _, span := range spans {
		s.names = append(s.names, span.Name())
	}
	return nil
}
func (s *traceSink) Shutdown(context.Context) error {
	s.closed.Add(1)
	return nil
}

func sampledSnapshot(name string) sdktrace.ReadOnlySpan {
	return tracetest.SpanStub{Name: name, SpanContext: traceapi.NewSpanContext(traceapi.SpanContextConfig{
		TraceID: traceapi.TraceID{1}, SpanID: traceapi.SpanID{1}, TraceFlags: traceapi.FlagsSampled,
	})}.Snapshot()
}

func TestTraceQueueRetainsFailedBatchesAndReportsCapacityLoss(t *testing.T) {
	ctx := context.Background()
	sink := &traceSink{fail: true}
	p, err := NewTraces(sink, 3, 2, 0, time.Second)
	check(t, err)
	provider := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(p), sdktrace.WithoutPanicRecording())
	for i := 0; i < 5; i++ {
		_, span := provider.Tracer(Scope).Start(ctx, fmt.Sprint(i))
		span.End()
	}
	if err := p.ForceFlush(ctx); !errors.Is(err, rejected) {
		t.Fatal(err)
	}
	if h := p.Health(); h.Pending != 3 || h.Dropped != 2 || h.Failed != 1 || h.Exported != 0 {
		t.Fatal(h)
	}
	sink.fail = false
	check(t, p.ForceFlush(ctx))
	if h := p.Health(); h.Pending != 0 || h.Exported != 3 || h.Attempts != 3 || h.Succeeded != 2 {
		t.Fatal(h)
	}
	if fmt.Sprint(sink.names) != "[0 1 2]" {
		t.Fatal(sink.names)
	}
	check(t, provider.Shutdown(ctx))
	check(t, p.Shutdown(ctx))
	if sink.closed.Load() != 1 || p.ForceFlush(ctx) == nil {
		t.Fatal("shutdown not retained")
	}
	p.OnEnd(sampledSnapshot("late"))
	if p.Health().Dropped != 3 {
		t.Fatal("late completion omitted from loss accounting")
	}
}

func TestTraceProcessorHonoursSDKRecordOnlySampling(t *testing.T) {
	sink := &traceSink{}
	p, err := NewTraces(sink, 8, 2, 0, time.Second)
	check(t, err)
	provider := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(p),
		sdktrace.WithSampler(sdktrace.AlwaysRecord(sdktrace.NeverSample())))
	_, span := provider.Tracer(Scope).Start(context.Background(), "record-only")
	if !span.IsRecording() || span.SpanContext().IsSampled() {
		t.Fatal("fixture did not produce record-only SDK span")
	}
	span.End()
	check(t, p.ForceFlush(context.Background()))
	if len(sink.names) != 0 || p.Health().Attempts != 0 || p.Health().Dropped != 0 {
		t.Fatal("exported a record-only span or counted sampling as loss", sink.names, p.Health())
	}
	check(t, provider.Shutdown(context.Background()))
}

func TestTraceDrainIsFiniteWithConcurrentProducersAndCancellation(t *testing.T) {
	sink := &traceSink{started: make(chan struct{}, 1), release: make(chan struct{})}
	p, err := NewTraces(sink, 8, 1, 0, time.Second)
	check(t, err)
	p.OnEnd(sampledSnapshot("first"))
	phase, cancel := context.WithCancel(context.Background())
	result := make(chan error, 1)
	go func() { result <- p.ForceFlush(phase) }()
	<-sink.started
	p.OnEnd(sampledSnapshot("second"))
	limited, stop := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer stop()
	if err := p.ForceFlush(limited); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal(err)
	}
	if err := p.Shutdown(limited); !errors.Is(err, context.DeadlineExceeded) || p.Health().Closed {
		t.Fatal(err)
	}
	cancel()
	if err := <-result; !errors.Is(err, context.Canceled) || p.Health().Pending != 2 {
		t.Fatal(err, p.Health())
	}
	close(sink.release)
	check(t, p.ForceFlush(context.Background()))
	check(t, p.Shutdown(context.Background()))
}

func TestTraceFlushDoesNotWaitForNewCompletions(t *testing.T) {
	sink := &traceSink{started: make(chan struct{}, 1), release: make(chan struct{})}
	p, err := NewTraces(sink, 8, 1, 0, time.Second)
	check(t, err)
	p.OnEnd(sampledSnapshot("first"))
	result := make(chan error, 1)
	go func() { result <- p.ForceFlush(context.Background()) }()
	<-sink.started
	p.OnEnd(sampledSnapshot("later"))
	close(sink.release)
	check(t, <-result)
	if h := p.Health(); h.Exported != 1 || h.Pending != 1 {
		t.Fatal("flush included a later completion", h)
	}
	check(t, p.Shutdown(context.Background()))
	if h := p.Health(); h.Exported != 2 || h.Pending != 0 {
		t.Fatal(h)
	}
}

func TestOwnedSchedulersDoNotCallGlobalHandlerOrReplaceProviders(t *testing.T) {
	previous := otel.GetErrorHandler()
	defer otel.SetErrorHandler(previous)
	var globalErrors atomic.Int64
	otel.SetErrorHandler(otel.ErrorHandlerFunc(func(error) { globalErrors.Add(1) }))
	applicationMeter, applicationTracer := otel.GetMeterProvider(), otel.GetTracerProvider()
	metricEvents := make(chan struct{}, 8)
	sink := &metricSink{fail: true, callback: func() {
		select {
		case metricEvents <- struct{}{}:
		default:
		}
	}}
	m, err := NewMetrics(metriccontrol.New(true, true), sink, resource.Empty(), time.Millisecond, time.Second)
	check(t, err)
	spans := &traceSink{started: make(chan struct{}, 8), release: make(chan struct{}), fail: true}
	close(spans.release)
	p, err := NewTraces(spans, 8, 2, time.Millisecond, time.Second)
	check(t, err)
	p.OnEnd(sampledSnapshot("retry"))
	for _, event := range []chan struct{}{metricEvents, spans.started} {
		select {
		case <-event:
		case <-time.After(time.Second):
			t.Fatal("scheduler did not run")
		}
	}
	if !errors.Is(m.Shutdown(context.Background()), rejected) || !errors.Is(p.Shutdown(context.Background()), rejected) {
		t.Fatal("shutdown suppressed failure")
	}
	<-m.worker.done
	<-p.worker.done
	if globalErrors.Load() != 0 || otel.GetMeterProvider() != applicationMeter || otel.GetTracerProvider() != applicationTracer {
		t.Fatal("owned failure changed application telemetry")
	}
	if m.Health().Failed == 0 || p.Health().Failed == 0 || p.Health().Pending != 1 {
		t.Fatal(m.Health(), p.Health())
	}
}

func TestInvalidSettingsAndCancelledFlush(t *testing.T) {
	for _, setting := range []struct {
		capacity, batch   int
		interval, timeout time.Duration
	}{
		{0, 1, 0, time.Second}, {1, 0, 0, time.Second}, {1, 2, 0, time.Second},
		{1, 1, -1, time.Second}, {1, 1, 0, 0},
	} {
		if _, err := NewTraces(&traceSink{}, setting.capacity, setting.batch, setting.interval, setting.timeout); err == nil {
			t.Fatal(setting)
		}
	}
	if _, err := NewTraces(nil, 1, 1, 0, time.Second); err == nil {
		t.Fatal("accepted missing exporter")
	}
	for _, interval := range []time.Duration{-1, 0} {
		if _, err := NewMetrics(nil, &metricSink{}, resource.Empty(), interval, time.Second); err == nil {
			t.Fatal("accepted missing controls")
		}
	}
	if _, err := NewMetrics(metriccontrol.New(true, true), nil, resource.Empty(), 0, time.Second); err == nil {
		t.Fatal("accepted missing exporter")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	p, err := NewTraces(&traceSink{}, 2, 1, 0, time.Second)
	check(t, err)
	if !errors.Is(p.ForceFlush(ctx), context.Canceled) {
		t.Fatal("ignored cancellation")
	}
	m, err := NewMetrics(metriccontrol.New(true, true), &metricSink{}, resource.Empty(), 0, time.Second)
	check(t, err)
	if !errors.Is(m.Flush(ctx), context.Canceled) {
		t.Fatal("ignored cancellation")
	}
	check(t, p.Shutdown(context.Background()))
	check(t, m.Shutdown(context.Background()))
}
