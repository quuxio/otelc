package probe

import (
	"context"
	"math"
	"runtime"
	"sync"
	"sync/atomic"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/metric"
	metricnoop "go.opentelemetry.io/otel/metric/noop"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	tracenoop "go.opentelemetry.io/otel/trace/noop"
	"io.quux.otelc/go/metriccontrol"
	"io.quux.otelc/go/pipeline"
)

type harness struct {
	probe    *Recorder
	controls *metriccontrol.Controls
	reader   *sdkmetric.ManualReader
	exporter *tracetest.InMemoryExporter
}

func newHarness(t *testing.T, collection bool, change func(*Options)) *harness {
	t.Helper()
	c := metriccontrol.New(collection, true)
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	tp := tracetest.NewInMemoryExporter()
	provider := sdktrace.NewTracerProvider(sdktrace.WithSyncer(tp), sdktrace.WithoutPanicRecording())
	options := Options{Functions: []string{"selected", "child"}, Lifetimes: []Lifetime{{"Object", true}, {"Plain", false}},
		MaxActive: 64, MaxLive: 64, Collecting: c.CollectionEnabled,
		Parent: func(ctx context.Context) context.Context { return ctx }, Detach: func(trace.Span) bool { return true }, Boundaries: []float64{.001, .01}}
	if change != nil {
		change(&options)
	}
	meter := c.MeterProvider(mp, metricnoop.NewMeterProvider(), func(string) bool { return true }).Meter(pipeline.Scope)
	p, err := New(meter, provider.Tracer(pipeline.Scope), options)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		p.Close()
		if err := provider.Shutdown(context.Background()); err != nil {
			t.Error(err)
		}
		if err := mp.Shutdown(context.Background()); err != nil {
			t.Error(err)
		}
	})
	return &harness{p, c, reader, tp}
}

func normal(p *Recorder)               { defer p.Start(0).End(false) }
func child(p *Recorder)                { defer p.Start(1).End(false) }
func panicking(p *Recorder, value any) { defer p.Start(0).End(false); panic(value) }
func internallyRecovered(p *Recorder) {
	defer p.Start(0).End(false)
	defer func() { _ = recover() }()
	panic(42)
}
func outerPanic(p *Recorder, value any) { defer p.Start(0).End(false); defer child(p); panic(value) }
func outerGoexit(p *Recorder)           { defer p.Start(0).End(false); defer child(p); runtime.Goexit() }

type hostileError struct{ calls *atomic.Int64 }

func (e *hostileError) Error() string { e.calls.Add(1); panic("must not format application error") }
func returnedError(p *Recorder) (err error) {
	invocation := p.Start(0)
	defer func() { invocation.End(err != nil) }()
	var typedNil *hostileError
	return typedNil
}

func catch(action func()) (value any) { defer func() { value = recover() }(); action(); return nil }

func TestFunctionOutcomesPreserveOriginalPanicAndIgnoreOuterUnwind(t *testing.T) {
	for _, scenario := range []struct {
		name   string
		action func(*Recorder)
		kinds  []string
	}{
		{"normal", normal, []string{""}}, {"recovered", internallyRecovered, []string{""}},
		{"panic", func(p *Recorder) { panicking(p, 42) }, []string{"panic"}},
		{"nil-panic", func(p *Recorder) { panicking(p, nil) }, []string{"panic"}},
		{"outer-panic", func(p *Recorder) { outerPanic(p, 42) }, []string{"", "panic"}},
		{"returned-typed-nil", func(p *Recorder) { _ = returnedError(p) }, []string{"returned_error"}},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			h := newHarness(t, true, nil)
			catch(func() { scenario.action(h.probe) })
			spans := h.exporter.GetSpans()
			if len(spans) != len(scenario.kinds) {
				t.Fatalf("spans: %d, wanted %d", len(spans), len(scenario.kinds))
			}
			for n, kind := range scenario.kinds {
				got := ""
				for _, attr := range spans[n].Attributes {
					if attr.Key == "error.type" {
						got = attr.Value.AsString()
					}
				}
				if got != kind || (kind != "" && spans[n].Status.Code != codes.Error) {
					t.Fatalf("span %d error %q, wanted %q", n, got, kind)
				}
			}
			if state := h.probe.Snapshot(); state.FunctionActive != 0 || state.UnwindUnknown != 0 || state.FunctionEnds != uint64(len(spans)) {
				t.Fatalf("bad health: %+v", state)
			}
		})
	}
	var calls atomic.Int64
	payload := &hostileError{&calls}
	h := newHarness(t, true, nil)
	if got := catch(func() { panicking(h.probe, payload) }); got != payload || calls.Load() != 0 {
		t.Fatal("changed or formatted original panic")
	}
}

func TestGoexitIsSeparateAndDeferredChildRemainsNormal(t *testing.T) {
	h := newHarness(t, true, nil)
	done := make(chan struct{})
	go func() { defer close(done); outerGoexit(h.probe) }()
	<-done
	spans := h.exporter.GetSpans()
	if len(spans) != 2 || spans[0].Status.Code != codes.Unset || spans[1].Status.Code != codes.Error {
		t.Fatalf("spans: %+v", spans)
	}
	if spans[1].Attributes[len(spans[1].Attributes)-1].Value.AsString() != "goexit" {
		t.Fatal("Goexit was not identified")
	}
}

func metricCount(t *testing.T, h *harness, name string) int64 {
	t.Helper()
	var data metricdata.ResourceMetrics
	if err := h.reader.Collect(context.Background(), &data); err != nil {
		t.Fatal(err)
	}
	var count int64
	for _, scope := range data.ScopeMetrics {
		for _, m := range scope.Metrics {
			if m.Name == name {
				for _, point := range m.Data.(metricdata.Sum[int64]).DataPoints {
					count += point.Value
				}
			}
		}
	}
	return count
}

func TestCachedMetricsRespectStartupAndMidCallSwitchesWithoutAffectingTraces(t *testing.T) {
	h := newHarness(t, false, nil)
	normal(h.probe)
	h.controls.SetCollection(true)
	normal(h.probe)
	func() { i := h.probe.Start(0); defer i.End(false); h.controls.SetCollection(false) }()
	h.controls.SetCollection(true)
	func() {
		h.controls.SetCollection(false)
		i := h.probe.Start(0)
		defer i.End(false)
		h.controls.SetCollection(true)
	}()
	if got := metricCount(t, h, "otelc.function.calls"); got != 1 {
		t.Fatalf("calls=%d wanted1", got)
	}
	if len(h.exporter.GetSpans()) != 4 {
		t.Fatal("collection changed tracing")
	}
}

func TestBoundsRepeatedExitCloseAndUnknownUnwind(t *testing.T) {
	h := newHarness(t, true, func(o *Options) { o.MaxActive = 1 })
	if h.probe.Start(-1) != nil || h.probe.Start(99) != nil {
		t.Fatal("invalid binding admitted")
	}
	i := h.probe.Start(0)
	if h.probe.Start(1) != nil {
		t.Fatal("capacity exceeded")
	}
	i.End(false)
	i.End(false)
	state := h.probe.Snapshot()
	if state.FunctionLoss != 3 || state.FunctionEnds != 1 || state.FunctionActive != 0 {
		t.Fatalf("state: %+v", state)
	}
	if unwind("missing.boundary") != "unknown" {
		t.Fatal("unknown stack falsely clean")
	}
	h.probe.Close()
	if h.probe.Start(0) != nil || !h.probe.Snapshot().Closed {
		t.Fatal("closed recorder admitted work")
	}
}

func recursiveFinish(i *Invocation, depth int) {
	if depth == 0 {
		i.End(false)
		return
	}
	recursiveFinish(i, depth-1)
	runtime.KeepAlive(depth)
}
func TestTruncatedUnwindStaysUnknown(t *testing.T) {
	h := newHarness(t, true, nil)
	i := h.probe.Start(0)
	recursiveFinish(i, 200)
	if h.probe.Snapshot().UnwindUnknown != 1 {
		t.Fatal("truncated stack falsely clean")
	}
	if h.exporter.GetSpans()[0].Status.Code != codes.Unset || metricCount(t, h, "otelc.function.errors") != 0 {
		t.Fatal("unknown unwind invented an application error")
	}
}

func TestConcurrentStartEndAndCloseAreBounded(t *testing.T) {
	h := newHarness(t, true, nil)
	var wg sync.WaitGroup
	for n := 0; n < 16; n++ {
		wg.Go(func() {
			for n := 0; n < 100; n++ {
				normal(h.probe)
			}
		})
	}
	wg.Wait()
	s := h.probe.Snapshot()
	if s.FunctionStarts != 1600 || s.FunctionEnds != 1600 || s.FunctionActive != 0 || s.FunctionLoss != 0 {
		t.Fatalf("bad concurrent accounting %+v", s)
	}
}

func TestValidationAndCopiedBindings(t *testing.T) {
	base := Options{Functions: []string{"one"}, Lifetimes: []Lifetime{{"T", true}}, MaxActive: 1, MaxLive: 1,
		Collecting: func() bool { return true }, Parent: func(ctx context.Context) context.Context { return ctx }, Detach: func(trace.Span) bool { return true }}
	meter := metricnoop.NewMeterProvider().Meter("fixture")
	tracer := tracenoop.NewTracerProvider().Tracer("fixture")
	for _, change := range []func(*Options){func(o *Options) { o.MaxActive = 0 }, func(o *Options) { o.MaxLive = 0 }, func(o *Options) { o.Collecting = nil }, func(o *Options) { o.Parent = nil }, func(o *Options) { o.Detach = nil }, func(o *Options) { o.Functions = []string{""} }, func(o *Options) { o.Functions = []string{"x", "x"} }, func(o *Options) { o.Lifetimes = []Lifetime{{"", false}} }, func(o *Options) { o.Lifetimes = []Lifetime{{"T", true}, {"T", true}} }} {
		o := base
		change(&o)
		if _, err := New(meter, tracer, o); err == nil {
			t.Fatal("invalid settings accepted")
		}
	}
	if _, err := New(nil, tracer, base); err == nil {
		t.Fatal("nil meter")
	}
	if _, err := New(meter, nil, base); err == nil {
		t.Fatal("nil tracer")
	}
	p, err := New(meter, tracer, base)
	if err != nil {
		t.Fatal(err)
	}
	base.Functions[0] = "changed"
	base.Lifetimes[0].Name = "changed"
	if p.options.Functions[0] != "one" || p.options.Lifetimes[0].Name != "T" {
		t.Fatal("selection was mutable")
	}
	var noInvocation *Invocation
	noInvocation.End(false)
	p.Close()
}

// errMeter exercises each instrument-construction failure without touching the
// application's global error handler or silently returning half-initialised probes.
type errMeter struct {
	metric.Meter
	target, seen int
}

func (m *errMeter) fail() error {
	m.seen++
	if m.seen == m.target {
		return context.Canceled
	}
	return nil
}
func (m *errMeter) Int64Counter(name string, opts ...metric.Int64CounterOption) (metric.Int64Counter, error) {
	if err := m.fail(); err != nil {
		return nil, err
	}
	return m.Meter.Int64Counter(name, opts...)
}
func (m *errMeter) Float64Histogram(name string, opts ...metric.Float64HistogramOption) (metric.Float64Histogram, error) {
	if err := m.fail(); err != nil {
		return nil, err
	}
	return m.Meter.Float64Histogram(name, opts...)
}
func (m *errMeter) Int64ObservableGauge(name string, opts ...metric.Int64ObservableGaugeOption) (metric.Int64ObservableGauge, error) {
	if err := m.fail(); err != nil {
		return nil, err
	}
	return m.Meter.Int64ObservableGauge(name, opts...)
}
func TestInstrumentConstructionFailure(t *testing.T) {
	for n := 1; n <= 9; n++ {
		m := &errMeter{Meter: metricnoop.NewMeterProvider().Meter("fixture"), target: n}
		_, err := New(m, tracenoop.NewTracerProvider().Tracer("fixture"), Options{MaxActive: 1, MaxLive: 1, Collecting: func() bool { return true }, Parent: func(ctx context.Context) context.Context { return ctx }, Detach: func(trace.Span) bool { return true }})
		if err != context.Canceled {
			t.Fatalf("instrument%d err%v", n, err)
		}
	}
}

func TestInvalidHistogramBoundariesFailBeforeSDKCalls(t *testing.T) {
	for _, boundaries := range [][]float64{{0}, {-1}, {1, 1}, {2, 1}, {math.NaN()}, {math.Inf(1)}} {
		o := Options{MaxActive: 1, MaxLive: 1, Collecting: func() bool { return true },
			Parent: func(ctx context.Context) context.Context { return ctx }, Detach: func(trace.Span) bool { return true }, Boundaries: boundaries}
		if _, err := New(metricnoop.NewMeterProvider().Meter("fixture"), tracenoop.NewTracerProvider().Tracer("fixture"), o); err == nil {
			t.Fatalf("invalid histogram accepted: %v", boundaries)
		}
	}
}

type applicationParent struct {
	tracenoop.Span
	context trace.SpanContext
	changes atomic.Uint64
}

func (p *applicationParent) SpanContext() trace.SpanContext      { return p.context }
func (p *applicationParent) End(...trace.SpanEndOption)          { p.changes.Add(1) }
func (p *applicationParent) SetStatus(codes.Code, string)        { p.changes.Add(1) }
func (p *applicationParent) SetAttributes(...attribute.KeyValue) { p.changes.Add(1) }

func TestTracingOffDoesNotEndOrMutateApplicationParent(t *testing.T) {
	parent := &applicationParent{context: trace.NewSpanContext(trace.SpanContextConfig{
		TraceID: trace.TraceID{1}, SpanID: trace.SpanID{2}, TraceFlags: trace.FlagsSampled, Remote: true})}
	p, err := New(metricnoop.NewMeterProvider().Meter("fixture"), tracenoop.NewTracerProvider().Tracer("fixture"), Options{
		Functions: []string{"selected"}, MaxActive: 1, MaxLive: 1, Collecting: func() bool { return false },
		Parent: func(ctx context.Context) context.Context { return trace.ContextWithSpan(ctx, parent) }, Detach: func(trace.Span) bool { return true }})
	if err != nil {
		t.Fatal(err)
	}
	defer p.Close()
	catch(func() { panicking(p, 42) })
	if parent.changes.Load() != 0 {
		t.Fatal("disabled tracing ended or mutated the application's parent span")
	}
}
