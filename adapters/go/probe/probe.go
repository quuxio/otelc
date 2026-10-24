// Package probe records generated source-free function and lifetime hooks in
// otelc's owned SDK. It does not replace application providers or recover panics.
package probe

import (
	"context"
	"errors"
	"math"
	"runtime"
	"sync"
	"sync/atomic"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/trace"
)

// Lifetime selects a resolved concrete pointer type, not arbitrary runtime names.
type Lifetime struct {
	Name     string
	Resource bool
}

// Options contains resolved selection and owned context callbacks. Generated
// hooks supply the official compiler's context bridge; ordinary app code does not.
type Options struct {
	Functions  []string
	Lifetimes  []Lifetime
	MaxActive  int
	MaxLive    int
	Collecting func() bool
	Parent     func() trace.Span
	Detach     func(trace.Span) bool
	Boundaries []float64
}

// Health keeps unobserved lifetimes and bounded observer failures visible.
type Health struct {
	FunctionStarts, FunctionEnds, FunctionActive, FunctionLoss uint64
	UnwindUnknown, ObjectsObserved, GCObserved, Released       uint64
	LiveObjects, OpenResources, LifetimeLoss, DuplicateObjects uint64
	Closed                                                     bool
}

// Recorder owns bounded bookkeeping and cached instruments. Collection gates
// wrap these instruments; tracing and lifecycle bookkeeping remain independent.
type Recorder struct {
	tracer           trace.Tracer
	options          Options
	calls            metric.Int64Counter
	errors           metric.Int64Counter
	duration         metric.Float64Histogram
	admitted         metric.Int64Counter
	completed        metric.Int64Counter
	incomplete       metric.Int64Counter
	lifetimeLost     metric.Int64Counter
	lifetimeDuration metric.Float64Histogram
	starts           atomic.Uint64
	ends             atomic.Uint64
	active           atomic.Uint64
	lost             atomic.Uint64
	unknown          atomic.Uint64
	mu               sync.Mutex
	closed           bool
	objects          map[any]*lifetime
	observed         uint64
	collected        uint64
	releases         uint64
	lifeLost         uint64
	duplicates       uint64
	abandoned        uint64
	unreleased       uint64
}

func validName(name string) bool { return name != "" && len(name) <= 4096 }

func (o Options) validate() error {
	if o.MaxActive <= 0 || o.MaxActive > 512 || o.MaxLive <= 0 || o.MaxLive > 1_000_000 || o.Collecting == nil || o.Parent == nil || o.Detach == nil ||
		len(o.Functions) > 4096 || len(o.Lifetimes) > 4096 || len(o.Boundaries) > 128 {
		return errors.New("invalid resolved Go probe settings")
	}
	seen := make(map[string]bool)
	for _, name := range o.Functions {
		if !validName(name) || seen[name] {
			return errors.New("invalid or duplicate Go function binding")
		}
		seen[name] = true
	}
	clear(seen)
	for _, binding := range o.Lifetimes {
		if !validName(binding.Name) || seen[binding.Name] {
			return errors.New("invalid or duplicate Go lifetime binding")
		}
		seen[binding.Name] = true
	}
	previous := 0.0
	for _, value := range o.Boundaries {
		if math.IsNaN(value) || math.IsInf(value, 0) || value <= previous {
			return errors.New("invalid Go probe histogram boundaries")
		}
		previous = value
	}
	return nil
}

// New receives an already controlled meter and an isolated official SDK tracer.
func New(meter metric.Meter, tracer trace.Tracer, options Options) (*Recorder, error) {
	if meter == nil || tracer == nil {
		return nil, errors.New("missing owned Go probe SDK")
	}
	if err := options.validate(); err != nil {
		return nil, err
	}
	options.Functions = append([]string(nil), options.Functions...)
	options.Lifetimes = append([]Lifetime(nil), options.Lifetimes...)
	options.Boundaries = append([]float64(nil), options.Boundaries...)
	p := &Recorder{tracer: tracer, options: options, objects: make(map[any]*lifetime)}
	var err error
	p.calls, err = meter.Int64Counter("otelc.function.calls", metric.WithUnit("{call}"))
	if err != nil {
		return nil, err
	}
	p.errors, err = meter.Int64Counter("otelc.function.errors", metric.WithUnit("{error}"))
	if err != nil {
		return nil, err
	}
	histogram := []metric.Float64HistogramOption{metric.WithUnit("s"), metric.WithExplicitBucketBoundaries(options.Boundaries...)}
	p.duration, err = meter.Float64Histogram("otelc.function.duration", histogram...)
	if err != nil {
		return nil, err
	}
	p.admitted, err = meter.Int64Counter("otelc.lifetime.admitted")
	if err != nil {
		return nil, err
	}
	p.completed, err = meter.Int64Counter("otelc.lifetime.completed")
	if err != nil {
		return nil, err
	}
	p.incomplete, err = meter.Int64Counter("otelc.lifetime.incomplete")
	if err != nil {
		return nil, err
	}
	p.lifetimeLost, err = meter.Int64Counter("otelc.lifetime.lost")
	if err != nil {
		return nil, err
	}
	p.lifetimeDuration, err = meter.Float64Histogram("otelc.lifetime.duration", histogram...)
	if err != nil {
		return nil, err
	}
	_, err = meter.Int64ObservableGauge("otelc.lifetime.live", metric.WithInt64Callback(func(ctx context.Context, observer metric.Int64Observer) error {
		state := p.Snapshot()
		observer.Observe(int64(state.LiveObjects), metric.WithAttributes(attribute.String("type", "all"), attribute.String("boundary", "observed_gc")))
		observer.Observe(int64(state.OpenResources), metric.WithAttributes(attribute.String("type", "all"), attribute.String("boundary", "resource")))
		return nil
	}))
	if err != nil {
		return nil, err
	}
	return p, nil
}

func (p *Recorder) parent() context.Context {
	ctx := context.Background()
	if parent := p.options.Parent(); parent != nil {
		ctx = trace.ContextWithSpan(ctx, parent)
	}
	return ctx
}

// Invocation is created by generated entry code and completed by a defer.
// The application result/error is never retained, formatted or unwrapped.
type Invocation struct {
	owner    *Recorder
	span     trace.Span
	ctx      context.Context
	started  time.Time
	function string
	boundary string
	collect  bool
	ended    atomic.Bool
}

// Start must be called directly in the selected function, so its actual caller
// identifies the unwind boundary even for methods, generics and recursive calls.
func (p *Recorder) Start(id int) *Invocation {
	p.mu.Lock()
	if p.closed {
		p.mu.Unlock()
		return nil
	}
	if id < 0 || id >= len(p.options.Functions) || p.active.Load() >= uint64(p.options.MaxActive) {
		p.lost.Add(1)
		p.mu.Unlock()
		return nil
	}
	p.active.Add(1)
	p.mu.Unlock()
	name := p.options.Functions[id]
	ctx, span := p.tracer.Start(p.parent(), name, trace.WithAttributes(attribute.String("function", name)))
	boundary := ""
	if pc, _, _, ok := runtime.Caller(1); ok {
		if function := runtime.FuncForPC(pc); function != nil {
			boundary = function.Name()
		}
	}
	p.starts.Add(1)
	return &Invocation{owner: p, span: span, ctx: ctx, started: time.Now(),
		function: name, boundary: boundary, collect: p.options.Collecting()}
}

// End records only a generated boolean test of a declared error return. It does
// not call error.Error, recover, rethrow, or alter the returned interface value.
// Only a known normal completion returns true for a generated release hook.
func (i *Invocation) End(returnedError bool) bool {
	if i == nil || i.ended.Swap(true) {
		return false
	}
	kind := unwind(i.boundary)
	p := i.owner
	if kind == "unknown" {
		p.unknown.Add(1)
	}
	if returnedError && kind == "normal" {
		kind = "returned_error"
	}
	failed := kind == "panic" || kind == "goexit" || kind == "returned_error"
	if kind == "unknown" {
		i.span.SetAttributes(attribute.String("otelc.unwind", kind))
	}
	if failed {
		i.span.SetAttributes(attribute.String("error.type", kind))
		i.span.SetStatus(codes.Error, "")
	}
	if i.collect {
		labels := metric.WithAttributes(attribute.String("function", i.function))
		p.calls.Add(i.ctx, 1, labels)
		p.duration.Record(i.ctx, time.Since(i.started).Seconds(), labels)
		if failed {
			p.errors.Add(i.ctx, 1, metric.WithAttributes(attribute.String("function", i.function), attribute.String("error.type", kind)))
		}
	}
	i.span.End()
	p.ends.Add(1)
	p.active.Add(^uint64(0))
	return kind == "normal"
}

// Snapshot does not drain events, invoke GC or change either metric switch.
func (p *Recorder) Snapshot() Health {
	p.mu.Lock()
	defer p.mu.Unlock()
	resources := p.unreleased
	for _, object := range p.objects {
		if object.resource != nil {
			resources++
		}
	}
	return Health{FunctionStarts: p.starts.Load(), FunctionEnds: p.ends.Load(), FunctionActive: p.active.Load(),
		FunctionLoss: p.lost.Load(), UnwindUnknown: p.unknown.Load(), ObjectsObserved: p.observed,
		GCObserved: p.collected, Released: p.releases, LiveObjects: uint64(len(p.objects)) + p.abandoned,
		OpenResources: resources, LifetimeLoss: p.lifeLost, DuplicateObjects: p.duplicates, Closed: p.closed}
}

// Close fences new probes and stops owned cleanup observations. Open GC/resource
// lifetimes stay unresolved in health; shutdown is never a synthetic completion.
func (p *Recorder) Close() {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.closed {
		return
	}
	p.closed = true
	p.abandoned = uint64(len(p.objects))
	for _, object := range p.objects {
		object.cleanup.Stop()
		if object.resource != nil {
			p.unreleased++
		}
	}
	clear(p.objects)
}
