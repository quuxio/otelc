package metriccontrol

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"sync"
	"testing"
	"time"

	api "go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/metric/noop"
	sdk "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

func collect(t *testing.T, reader *sdk.ManualReader) *metricdata.ResourceMetrics {
	t.Helper()
	data := &metricdata.ResourceMetrics{}
	if err := reader.Collect(context.Background(), data); err != nil {
		t.Fatal(err)
	}
	return data
}

func values(data *metricdata.ResourceMetrics) map[string]float64 {
	result := map[string]float64{}
	for _, scope := range data.ScopeMetrics {
		for _, item := range scope.Metrics {
			var total float64
			switch points := item.Data.(type) {
			case metricdata.Sum[int64]:
				for _, point := range points.DataPoints {
					total += float64(point.Value)
				}
			case metricdata.Sum[float64]:
				for _, point := range points.DataPoints {
					total += point.Value
				}
			case metricdata.Gauge[int64]:
				for _, point := range points.DataPoints {
					total += float64(point.Value)
				}
			case metricdata.Gauge[float64]:
				for _, point := range points.DataPoints {
					total += point.Value
				}
			case metricdata.Histogram[int64]:
				for _, point := range points.DataPoints {
					total += float64(point.Count)
				}
			case metricdata.Histogram[float64]:
				for _, point := range points.DataPoints {
					total += float64(point.Count)
				}
			}
			result[item.Name] = total
		}
	}
	return result
}

func setup(t *testing.T, c *Controls) (api.MeterProvider, *sdk.ManualReader, *sdk.ManualReader) {
	t.Helper()
	reader := sdk.NewManualReader()
	base := sdk.NewMeterProvider(sdk.WithReader(reader))
	t.Cleanup(func() {
		if err := base.Shutdown(context.Background()); err != nil {
			t.Error(err)
		}
	})
	applicationReader := sdk.NewManualReader()
	application := sdk.NewMeterProvider(sdk.WithReader(applicationReader))
	t.Cleanup(func() {
		if err := application.Shutdown(context.Background()); err != nil {
			t.Error(err)
		}
	})
	return c.MeterProvider(base, application, func(scope string) bool { return scope == "owned" }), reader, applicationReader
}

func recorders(t *testing.T, meter api.Meter) []func(int64) {
	t.Helper()
	ctx := context.Background()
	a, e1 := meter.Int64Counter("i.counter", api.WithUnit("call"))
	b, e2 := meter.Int64UpDownCounter("i.updown")
	d, e3 := meter.Int64Histogram("i.histogram", api.WithExplicitBucketBoundaries(1, 5, 10))
	e, e4 := meter.Int64Gauge("i.gauge")
	f, e5 := meter.Float64Counter("f.counter")
	g, e6 := meter.Float64UpDownCounter("f.updown")
	h, e7 := meter.Float64Histogram("f.histogram")
	i, e8 := meter.Float64Gauge("f.gauge")
	for _, err := range []error{e1, e2, e3, e4, e5, e6, e7, e8} {
		if err != nil {
			t.Fatal(err)
		}
	}
	return []func(int64){
		func(n int64) { a.Add(ctx, n) }, func(n int64) { b.Add(ctx, n) },
		func(n int64) { d.Record(ctx, n) }, func(n int64) { e.Record(ctx, n) },
		func(n int64) { f.Add(ctx, float64(n)) }, func(n int64) { g.Add(ctx, float64(n)) },
		func(n int64) { h.Record(ctx, float64(n)) }, func(n int64) { i.Record(ctx, float64(n)) },
	}
}

func TestCachedInstrumentsAndIndependentSwitchesFromEveryInitialState(t *testing.T) {
	for _, initialCollection := range []bool{false, true} {
		for _, initialEmission := range []bool{false, true} {
			t.Run(fmt.Sprint(initialCollection, "/", initialEmission), func(t *testing.T) {
				c := New(initialCollection, initialEmission)
				provider, reader, _ := setup(t, c)
				records := recorders(t, provider.Meter("owned"))
				record := func(value int64) {
					for _, fn := range records {
						fn(value)
					}
				}
				target := &exportSpy{}
				sender := c.Exporter(target)
				if sender.Temporality(sdk.InstrumentKindCounter) != metricdata.CumulativeTemporality {
					t.Fatal("lost cumulative retention")
				}
				record(1)
				if err := sender.Export(context.Background(), collect(t, reader)); err != nil {
					t.Fatal(err)
				}
				if got := target.count(); got != map[bool]int{true: 1, false: 0}[initialEmission] {
					t.Fatal(got)
				}
				c.SetCollection(false)
				if err := c.SetEmission(context.Background(), true); err != nil {
					t.Fatal(err)
				}
				record(100)
				before := values(collect(t, reader))
				for _, name := range []string{"i.counter", "i.updown", "i.gauge", "i.histogram", "f.counter", "f.updown", "f.gauge", "f.histogram"} {
					want := float64(0)
					if initialCollection {
						want = 1
					}
					if before[name] != want {
						t.Fatalf("disabled %s=%v, want %v", name, before[name], want)
					}
				}
				c.SetCollection(true)
				if err := c.SetEmission(context.Background(), false); err != nil {
					t.Fatal(err)
				}
				record(2)
				count := target.count()
				if err := sender.Export(context.Background(), collect(t, reader)); err != nil {
					t.Fatal(err)
				}
				if target.count() != count {
					t.Fatal("emission-off transmitted")
				}
				if err := c.SetEmission(context.Background(), true); err != nil {
					t.Fatal(err)
				}
				if err := sender.Export(context.Background(), collect(t, reader)); err != nil {
					t.Fatal(err)
				}
				got := target.last()
				want := float64(2)
				histogram := float64(1)
				if initialCollection {
					want++
					histogram++
				}
				for _, name := range []string{"i.counter", "i.updown", "f.counter", "f.updown"} {
					if got[name] != want {
						t.Fatalf("restored %s=%v, want %v", name, got[name], want)
					}
				}
				if got["i.gauge"] != 2 || got["f.gauge"] != 2 || got["i.histogram"] != histogram || got["f.histogram"] != histogram {
					t.Fatal(got)
				}
				if !c.CollectionEnabled() || !c.EmissionEnabled() {
					t.Fatal("unexpected state")
				}
			})
		}
	}
}

type exportSpy struct {
	mu                               sync.Mutex
	batches                          []map[string]float64
	started                          chan struct{}
	release                          chan struct{}
	exportErr, flushErr, shutdownErr error
	flushes, shutdowns               int
}

func (*exportSpy) Temporality(sdk.InstrumentKind) metricdata.Temporality {
	return metricdata.DeltaTemporality
}
func (*exportSpy) Aggregation(sdk.InstrumentKind) sdk.Aggregation { return sdk.AggregationSum{} }
func (s *exportSpy) Export(ctx context.Context, data *metricdata.ResourceMetrics) error {
	if s.started != nil {
		close(s.started)
		select {
		case <-s.release:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.batches = append(s.batches, values(data))
	return s.exportErr
}
func (s *exportSpy) ForceFlush(context.Context) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.flushes++
	return s.flushErr
}
func (s *exportSpy) Shutdown(context.Context) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.shutdowns++
	return s.shutdownErr
}
func (s *exportSpy) count() int { s.mu.Lock(); defer s.mu.Unlock(); return len(s.batches) }
func (s *exportSpy) last() map[string]float64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.batches[len(s.batches)-1]
}

func TestEmissionOffIsAnExportBarrierAndCancellationCannotAcknowledgeIt(t *testing.T) {
	c := New(true, true)
	target := &exportSpy{started: make(chan struct{}), release: make(chan struct{})}
	sender := c.Exporter(target)
	completed := make(chan error, 1)
	go func() { completed <- sender.Export(context.Background(), &metricdata.ResourceMetrics{}) }()
	<-target.started
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if err := c.SetEmission(ctx, false); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal(err)
	}
	if !c.EmissionEnabled() {
		t.Fatal("timed out request changed state")
	}
	disabled := make(chan error, 1)
	go func() { disabled <- c.SetEmission(context.Background(), false) }()
	select {
	case err := <-disabled:
		t.Fatalf("acknowledged in-flight export: %v", err)
	case <-time.After(20 * time.Millisecond):
	}
	close(target.release)
	if err := <-completed; err != nil {
		t.Fatal(err)
	}
	if err := <-disabled; err != nil {
		t.Fatal(err)
	}
	if c.EmissionEnabled() {
		t.Fatal("barrier did not switch off")
	}
	if err := sender.Export(context.Background(), &metricdata.ResourceMetrics{}); err != nil {
		t.Fatal(err)
	}
	if target.count() != 1 {
		t.Fatal("export after barrier")
	}
}

func TestExporterErrorsShutdownAndUnownedMetersKeepTheirContracts(t *testing.T) {
	c := New(false, false)
	provider, reader, applicationReader := setup(t, c)
	own, _ := provider.Meter("owned").Int64Counter("own")
	app, _ := provider.Meter("application").Int64Counter("app")
	own.Add(context.Background(), 9)
	app.Add(context.Background(), 7)
	if got := values(collect(t, reader)); len(got) != 0 {
		t.Fatal(got)
	}
	if got := values(collect(t, applicationReader)); !reflect.DeepEqual(got, map[string]float64{"app": 7}) {
		t.Fatal(got)
	}
	sentinel := errors.New("exporter failed")
	target := &exportSpy{exportErr: sentinel, flushErr: sentinel, shutdownErr: sentinel}
	sender := c.Exporter(target)
	if err := sender.ForceFlush(context.Background()); err != nil || target.flushes != 0 {
		t.Fatal(err)
	}
	if err := c.SetEmission(context.Background(), true); err != nil {
		t.Fatal(err)
	}
	if err := sender.Export(context.Background(), &metricdata.ResourceMetrics{}); !errors.Is(err, sentinel) {
		t.Fatal(err)
	}
	if err := sender.ForceFlush(context.Background()); !errors.Is(err, sentinel) {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		if err := sender.Shutdown(context.Background()); !errors.Is(err, sentinel) {
			t.Fatal(err)
		}
	}
	if target.shutdowns != 1 {
		t.Fatal(target.shutdowns)
	}
	if err := sender.Export(context.Background(), &metricdata.ResourceMetrics{}); !errors.Is(err, sdk.ErrExporterShutdown) {
		t.Fatal(err)
	}
	if err := sender.ForceFlush(context.Background()); !errors.Is(err, sdk.ErrExporterShutdown) {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	for _, operation := range []func(context.Context) error{sender.ForceFlush, sender.Shutdown, func(ctx context.Context) error { return sender.Export(ctx, &metricdata.ResourceMetrics{}) }} {
		if err := operation(ctx); !errors.Is(err, context.Canceled) {
			t.Fatal(err)
		}
	}
}

func TestTraceExportRemainsIndependent(t *testing.T) {
	c := New(false, false)
	spans := tracetest.NewInMemoryExporter()
	provider := sdktrace.NewTracerProvider(sdktrace.WithSyncer(spans))
	defer provider.Shutdown(context.Background())
	tracer := provider.Tracer("same-process")
	for _, state := range [][2]bool{{false, false}, {true, false}, {false, true}, {true, true}} {
		c.SetCollection(state[0])
		if err := c.SetEmission(context.Background(), state[1]); err != nil {
			t.Fatal(err)
		}
		_, span := tracer.Start(context.Background(), "function")
		span.End()
	}
	if len(spans.GetSpans()) != 4 {
		t.Fatal("metric state affected traces")
	}
}

func TestPeriodicReaderRetainsMeasurementsAcrossEmissionOff(t *testing.T) {
	c := New(true, false)
	target := &exportSpy{}
	sender := c.Exporter(target)
	reader := sdk.NewPeriodicReader(sender, sdk.WithInterval(time.Hour))
	base := sdk.NewMeterProvider(sdk.WithReader(reader))
	defer base.Shutdown(context.Background())
	meter := c.MeterProvider(base, noop.NewMeterProvider(), func(string) bool { return true }).Meter("owned")
	counter, err := meter.Int64Counter("calls")
	if err != nil {
		t.Fatal(err)
	}
	counter.Add(context.Background(), 3)
	if err := base.ForceFlush(context.Background()); err != nil {
		t.Fatal(err)
	}
	if target.count() != 0 {
		t.Fatal("disabled periodic exporter transmitted")
	}
	if err := c.SetEmission(context.Background(), true); err != nil {
		t.Fatal(err)
	}
	if err := base.ForceFlush(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := target.last()["calls"]; got != 3 {
		t.Fatalf("emission-off lost collected measurements: %v", got)
	}
	c.SetCollection(false)
	counter.Add(context.Background(), 100)
	if err := base.ForceFlush(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := target.last()["calls"]; got != 3 {
		t.Fatal(got)
	}
	c.SetCollection(true)
	counter.Add(context.Background(), 2)
	if err := base.ForceFlush(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := target.last()["calls"]; got != 5 {
		t.Fatal(got)
	}
}

func TestApplicationSDKKeepsEmissionAndDeltaTemporalityWhenOwnedMetricsAreOff(t *testing.T) {
	c := New(false, false)
	target := &exportSpy{}
	reader := sdk.NewPeriodicReader(target, sdk.WithInterval(time.Hour))
	application := sdk.NewMeterProvider(sdk.WithReader(reader))
	defer application.Shutdown(context.Background())
	own := sdk.NewMeterProvider()
	defer own.Shutdown(context.Background())
	provider := c.MeterProvider(own, application, func(name string) bool { return name == "owned" })
	counter, err := provider.Meter("application").Int64Counter("app.calls")
	if err != nil {
		t.Fatal(err)
	}
	counter.Add(context.Background(), 7)
	if err := application.ForceFlush(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := target.last()["app.calls"]; got != 7 {
		t.Fatal(got)
	}
	counter.Add(context.Background(), 2)
	if err := application.ForceFlush(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := target.last()["app.calls"]; got != 2 {
		t.Fatalf("application delta preference changed: %v", got)
	}
	if c.CollectionEnabled() || c.EmissionEnabled() {
		t.Fatal("owned controls changed")
	}
}
