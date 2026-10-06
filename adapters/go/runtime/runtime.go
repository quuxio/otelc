// Package runtime is injected into generated compiler input, never original source.
package runtime

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"sync"
	"sync/atomic"
	"time"

	"go.opentelemetry.io/otel/attribute"
	api "go.opentelemetry.io/otel/metric"
	sdk "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	"io.quux.otelc/go/policy"
)

type function struct {
	Count   uint64 `json:"count"`
	Unwinds uint64 `json:"unwinds"`
	options api.MeasurementOption
}
type frame struct {
	name  string
	start time.Time
}
type Runtime struct {
	plan           policy.Plan
	mu             sync.Mutex
	functions      map[string]*function
	pending        map[uint64]frame
	losses         map[string]uint64
	next           uint64
	enabled        atomic.Bool
	closed         atomic.Bool
	revision       atomic.Uint64
	exportLoss     atomic.Uint64
	provider       *sdk.MeterProvider
	calls, unwinds api.Int64Counter
	duration       api.Float64Histogram
	control        *control
	finished       bool
}

var active atomic.Pointer[Runtime]

func init() {
	path := os.Getenv("OTELC_GO_PLAN")
	if path == "" {
		return
	}
	os.Unsetenv("OTELC_GO_PLAN") // Child processes must be explicitly instrumented.
	plan, err := policy.Load(path)
	if err == nil && policy.LegacyNilPanic(os.Getenv("GODEBUG")) {
		err = fmt.Errorf("go panicnil=1 is unsupported")
	}
	var rt *Runtime
	if err == nil {
		rt, err = New(plan)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "otelc Go:", err)
		os.Exit(2)
	}
	active.Store(rt)
}
func New(plan policy.Plan) (*Runtime, error) {
	rt := &Runtime{plan: plan, functions: map[string]*function{}, pending: map[uint64]frame{}, losses: map[string]uint64{"function_capacity": 0, "active_call_capacity": 0, "incomplete": 0, "invalid": 0, "unsupported_runtime": 0}}
	rt.enabled.Store(plan.Metrics.Enabled)
	exporter, err := newExporter(rt)
	if err != nil {
		return nil, err
	}
	reader := sdk.NewPeriodicReader(exporter, sdk.WithInterval(time.Duration(plan.Export.IntervalMS)*time.Millisecond), sdk.WithTimeout(time.Duration(min(plan.Export.TimeoutMS, plan.Runtime.ShutdownMS))*time.Millisecond))
	attrs := []attribute.KeyValue{attribute.String("service.name", plan.Resource.Name), attribute.String("service.version", plan.Resource.Version), attribute.String("service.instance.id", fmt.Sprint(os.Getpid()))}
	for key, value := range plan.Resource.Attributes {
		attrs = append(attrs, attribute.String(key, value))
	}
	rt.provider = sdk.NewMeterProvider(sdk.WithReader(reader), sdk.WithResource(resource.NewSchemaless(attrs...)), sdk.WithCardinalityLimit(max(plan.Runtime.MaxFunctions+1, 8)), sdk.WithView(sdk.NewView(sdk.Instrument{Name: "otelc.function.duration"}, sdk.Stream{Aggregation: sdk.AggregationExplicitBucketHistogram{Boundaries: plan.Metrics.Bounds}})))
	meter := rt.provider.Meter("quux.otelc", api.WithInstrumentationVersion("0.1.0"))
	rt.calls, _ = meter.Int64Counter("otelc.function.calls", api.WithUnit("{call}"))
	rt.unwinds, _ = meter.Int64Counter("otelc.function.unwinds", api.WithUnit("{observation}"))
	rt.duration, _ = meter.Float64Histogram("otelc.function.duration", api.WithUnit("s"))
	dropped, _ := meter.Int64ObservableCounter("otelc.runtime.dropped_observations", api.WithUnit("{observation}"))
	exports, _ := meter.Int64ObservableCounter("otelc.export.dropped_batches", api.WithUnit("{batch}"))
	_, err = meter.RegisterCallback(func(ctx context.Context, observer api.Observer) error {
		rt.mu.Lock()
		defer rt.mu.Unlock()
		for reason, value := range rt.losses {
			observer.ObserveInt64(dropped, int64(value), api.WithAttributes(attribute.String("reason", reason)))
		}
		observer.ObserveInt64(exports, int64(rt.exportLoss.Load()))
		return nil
	}, dropped, exports)
	if err == nil && plan.Runtime.Socket != nil {
		rt.control, err = bind(rt, *plan.Runtime.Socket)
	}
	if err != nil {
		rt.provider.Shutdown(context.Background())
		return nil, err
	}
	return rt, nil
}
func (rt *Runtime) start(name string) uint64 {
	if !rt.enabled.Load() || rt.closed.Load() {
		return 0
	}
	rt.mu.Lock()
	defer rt.mu.Unlock()
	if rt.closed.Load() {
		return 0
	}
	if _, ok := rt.functions[name]; !ok {
		if len(rt.functions) >= rt.plan.Runtime.MaxFunctions || len(name) > 1024 {
			rt.losses["function_capacity"]++
			rt.revision.Add(1)
			return 0
		}
		rt.functions[name] = &function{options: api.WithAttributes(attribute.String("code.function.name", name))}
	}
	if len(rt.pending) >= rt.plan.Runtime.MaxActive {
		rt.losses["active_call_capacity"]++
		rt.revision.Add(1)
		return 0
	}
	rt.next++
	if rt.next == 0 {
		rt.losses["invalid"]++
		rt.revision.Add(1)
		return 0
	}
	rt.pending[rt.next] = frame{name: name, start: time.Now()}
	return rt.next
}
func (rt *Runtime) finish(token uint64, unwound bool) {
	ended := time.Now()
	rt.mu.Lock()
	defer rt.mu.Unlock()
	value, ok := rt.pending[token]
	if !ok {
		return
	}
	delete(rt.pending, token)
	fn := rt.functions[value.name]
	fn.Count++
	if unwound {
		fn.Unwinds++
	}
	rt.calls.Add(context.Background(), 1, fn.options)
	rt.duration.Record(context.Background(), ended.Sub(value.start).Seconds(), fn.options)
	if unwound {
		rt.unwinds.Add(context.Background(), 1, fn.options)
	}
	rt.revision.Add(1)
}
func Start(name string) uint64 {
	if rt := active.Load(); rt != nil {
		return rt.start(name)
	}
	return 0
}

// Finish must itself be the deferred function: direct recover preserves the value.
func Finish(token uint64) {
	if token == 0 {
		return
	}
	rt := active.Load()
	if rt == nil {
		return
	}
	if policy.LegacyNilPanic(os.Getenv("GODEBUG")) {
		rt.mu.Lock()
		delete(rt.pending, token)
		rt.losses["unsupported_runtime"]++
		rt.revision.Add(1)
		rt.mu.Unlock()
		return // Do not recover and accidentally swallow a legacy nil panic.
	}
	value := recover()
	rt.finish(token, value != nil)
	if value != nil {
		panic(value)
	}
}
func Close() {
	if rt := active.Load(); rt != nil {
		rt.Close()
	}
}
func (rt *Runtime) report() map[string]any {
	rt.mu.Lock()
	defer rt.mu.Unlock()
	count := uint64(0)
	functions := map[string]any{}
	lost := map[string]uint64{}
	for name, fn := range rt.functions {
		count += fn.Count
		functions[name] = map[string]uint64{"count": fn.Count, "unwinds": fn.Unwinds}
	}
	for name, value := range rt.losses {
		lost[name] = value
	}
	lost["incomplete"] += uint64(len(rt.pending))
	return map[string]any{"schema_version": 1, "language": "go", "pid": os.Getpid(), "export_finished": rt.finished, "function_calls": count, "functions": functions, "losses": lost, "export_loss": rt.exportLoss.Load()}
}
func (rt *Runtime) Close() {
	if !rt.closed.CompareAndSwap(false, true) {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(rt.plan.Runtime.ShutdownMS)*time.Millisecond)
	defer cancel()
	if rt.control != nil {
		rt.control.close()
	}
	rt.mu.Lock()
	rt.losses["incomplete"] += uint64(len(rt.pending))
	clear(rt.pending)
	rt.revision.Add(1)
	rt.mu.Unlock()
	flushed := rt.provider.ForceFlush(ctx)
	stopped := rt.provider.Shutdown(ctx)
	rt.mu.Lock()
	rt.finished = ctx.Err() == nil
	rt.mu.Unlock()
	if flushed != nil || stopped != nil {
		rt.exportLoss.Add(1)
	}
	if path := os.Getenv("OTELC_REPORT_PATH"); path != "" {
		if data, err := json.MarshalIndent(rt.report(), "", "  "); err == nil {
			os.WriteFile(path, append(data, '\n'), 0600)
		}
	}
}
