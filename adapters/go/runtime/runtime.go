// Package runtime owns the SDK, controls and final checkpoint for generated Go
// instrumentation. It never installs global application providers or handlers.
package runtime

import (
	"context"
	"errors"
	"math"
	"os"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
	"unicode/utf8"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
	"go.opentelemetry.io/otel/trace/noop"
	"io.quux.otelc/go/metriccontrol"
	"io.quux.otelc/go/pipeline"
	"io.quux.otelc/go/probe"
)

// Config is the fixed producer-owned translation of the shared TOML. The
// developer does not author this structure or any SDK-specific configuration.
type Config struct {
	SchemaVersion     int              `json:"schema_version"`
	ServiceName       string           `json:"service_name"`
	Endpoint          string           `json:"endpoint"`
	IntervalMS        int              `json:"interval_ms"`
	TimeoutMS         int              `json:"timeout_ms"`
	ShutdownTimeoutMS int              `json:"shutdown_timeout_ms"`
	Collection        bool             `json:"collection"`
	Emission          bool             `json:"emission"`
	TracesEnabled     bool             `json:"traces_enabled"`
	HTTPContext       bool             `json:"http_context"`
	SampleRatio       float64          `json:"sample_ratio"`
	Functions         []string         `json:"functions"`
	Lifetimes         []probe.Lifetime `json:"lifetimes"`
	MaxLive           int              `json:"max_live"`
	Boundaries        []float64        `json:"boundaries"`
	PolicySHA256      string           `json:"policy_sha256"`
	SelectionSHA256   string           `json:"selection_sha256"`
}

var digestPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

func (c Config) validate() error {
	if c.SchemaVersion != 2 || c.ServiceName == "" || utf8.RuneCountInString(c.ServiceName) > 128 ||
		!digestPattern.MatchString(c.PolicySHA256) || !digestPattern.MatchString(c.SelectionSHA256) ||
		math.IsNaN(c.SampleRatio) || math.IsInf(c.SampleRatio, 0) || c.SampleRatio < 0 || c.SampleRatio > 1 {
		return errors.New("invalid Go runtime identity or sampling configuration")
	}
	for _, char := range c.ServiceName {
		if char < 32 {
			return errors.New("invalid Go service name")
		}
	}
	for _, value := range []int{c.IntervalMS, c.TimeoutMS, c.ShutdownTimeoutMS} {
		if value < 1 || value > 1_000_000 {
			return errors.New("invalid Go SDK timing budget")
		}
	}
	return c.probeOptions(func(ctx context.Context) context.Context { return ctx }, func(trace.Span) bool { return true }).Validate()
}

func (c Config) probeOptions(parent func(context.Context) context.Context, detach func(trace.Span) bool) probe.Options {
	return probe.Options{Functions: c.Functions, Lifetimes: c.Lifetimes, MaxActive: 512, MaxLive: c.MaxLive,
		Collecting: func() bool { return false }, Parent: parent, Detach: detach, Boundaries: c.Boundaries}
}

// Runtime keeps one embedded SDK and application PID through all live states.
type Runtime struct {
	Config                 Config
	Controls               *metriccontrol.Controls
	Probes                 *probe.Recorder
	Metrics                *pipeline.Metrics
	Traces                 *pipeline.Traces
	provider               *sdktrace.TracerProvider
	server                 *controlServer
	directory              string
	mu                     sync.Mutex
	phase, flush, shutdown string
	flushAttempts          atomic.Uint64
	flushFailures          atomic.Uint64
	closeOnce              sync.Once
	closeErr               error
}

// TracerProvider exposes this runtime's owned SDK to generated library hooks.
// It never returns or installs an application-global provider.
func (r *Runtime) TracerProvider() trace.TracerProvider {
	if r.provider != nil {
		return r.provider
	}
	return noop.NewTracerProvider()
}

// RejectAmbient refuses before creating an official SDK that could parse/log
// ambient SDK defaults. It does not change the application's environment.
func RejectAmbient() error {
	for _, item := range os.Environ() {
		name, _, _ := strings.Cut(item, "=")
		if strings.HasPrefix(name, "OTEL_") {
			return errors.New("Go profile requires unset ambient OTEL_* SDK variables")
		}
	}
	return nil
}

// New starts an owned pipeline in an existing private launch directory. Context
// callbacks come from the pinned official compiler bridge, not application code.
func New(config Config, directory string, parent func(context.Context) context.Context, detach func(trace.Span) bool) (*Runtime, error) {
	if err := config.validate(); err != nil {
		return nil, err
	}
	if err := config.probeOptions(parent, detach).Validate(); err != nil {
		return nil, err
	}
	if err := RejectAmbient(); err != nil {
		return nil, err
	}
	if err := privateDirectory(directory); err != nil {
		return nil, err
	}
	for _, name := range []string{"health.json", "control.json"} {
		if _, err := os.Lstat(directory + string(os.PathSeparator) + name); !os.IsNotExist(err) {
			return nil, errors.New("Go runtime outputs must be new")
		}
	}
	r := &Runtime{Config: config, directory: directory, Controls: metriccontrol.New(config.Collection, config.Emission),
		phase: "running", flush: "not_started", shutdown: "not_started"}
	if err := r.attach(parent, detach); err != nil {
		r.discard()
		return nil, err
	}
	if err := createJSON(directory, "health.json", r.Health()); err != nil {
		r.discard()
		return nil, err
	}
	server, err := newControl(r)
	if err != nil {
		r.discard()
		return nil, err
	}
	r.server = server
	return r, nil
}

func (r *Runtime) attach(parent func(context.Context) context.Context, detach func(trace.Span) bool) error {
	c := r.Config
	res := resource.NewSchemaless(attribute.String("service.name", c.ServiceName),
		attribute.String("telemetry.sdk.language", "go"), attribute.String("telemetry.sdk.name", "opentelemetry"),
		attribute.String("telemetry.sdk.version", otel.Version()))
	timeout := time.Duration(c.TimeoutMS) * time.Millisecond
	interval := time.Duration(c.IntervalMS) * time.Millisecond
	options := pipeline.HTTPOptions{Endpoint: strings.TrimRight(c.Endpoint, "/") + "/v1/metrics", Timeout: timeout}
	exporter, err := pipeline.NewHTTPMetrics(context.Background(), options)
	if err != nil {
		return err
	}
	r.Metrics, err = pipeline.NewMetrics(r.Controls, exporter, res, interval, timeout)
	if err != nil {
		_ = exporter.Shutdown(context.Background())
		return err
	}
	var tracer trace.Tracer = noop.NewTracerProvider().Tracer(pipeline.Scope)
	if c.TracesEnabled {
		options.Endpoint = strings.TrimRight(c.Endpoint, "/") + "/v1/traces"
		exporter, err := pipeline.NewHTTPTraces(context.Background(), options)
		if err != nil {
			return err
		}
		r.Traces, err = pipeline.NewTraces(exporter, 8192, 256, interval, timeout)
		if err != nil {
			_ = exporter.Shutdown(context.Background())
			return err
		}
		r.provider = sdktrace.NewTracerProvider(sdktrace.WithResource(res), sdktrace.WithSpanProcessor(r.Traces),
			sdktrace.WithSampler(sdktrace.ParentBased(sdktrace.TraceIDRatioBased(c.SampleRatio))), sdktrace.WithoutPanicRecording())
		tracer = r.provider.Tracer(pipeline.Scope)
	} else if c.HTTPContext {
		// Preserve implicit W3C context even when span recording/export is
		// disabled. No processor or exporter is created for this provider.
		r.provider = sdktrace.NewTracerProvider(sdktrace.WithResource(res), sdktrace.WithSampler(sdktrace.NeverSample()),
			sdktrace.WithoutPanicRecording())
		tracer = r.provider.Tracer(pipeline.Scope)
	}
	probes := c.probeOptions(parent, detach)
	probes.Collecting = r.Controls.CollectionEnabled
	r.Probes, err = probe.New(r.Metrics.Provider.Meter(pipeline.Scope), tracer, probes)
	return err
}

// Flush is independent of tracing configuration and preserves failed export
// evidence. Collection-off must not transmit a cached metric snapshot.
func (r *Runtime) Flush(ctx context.Context) error {
	r.flushAttempts.Add(1)
	err := r.Metrics.Flush(ctx)
	if r.provider != nil {
		err = errors.Join(err, r.provider.ForceFlush(ctx))
	}
	if err != nil {
		r.flushFailures.Add(1)
	}
	return err
}

// Status is the existing language-independent metric-control response.
func (r *Runtime) Status() map[string]any {
	return map[string]any{"schema_version": 1, "pid": os.Getpid(), "collection": r.Controls.CollectionEnabled(),
		"emission": r.Controls.EmissionEnabled(), "effective_emission_off": !r.Controls.EmissionEnabled()}
}

// Health reports raw owned API counters and pending observations. It does not
// claim Collector receipt, complete application closure or invocation attribution.
func (r *Runtime) Health() map[string]any {
	r.mu.Lock()
	phase, flush, shutdown := r.phase, r.flush, r.shutdown
	r.mu.Unlock()
	var observers probe.Health
	if r.Probes != nil {
		observers = r.Probes.Snapshot()
	}
	metrics := map[string]any{"observed": false}
	if r.Metrics != nil {
		metrics = exportHealth(r.Metrics.Health(), false)
	}
	traces := map[string]any{"observed": false}
	httpSpans := map[string]any{"observed": false}
	if r.Traces != nil {
		traces = exportHealth(r.Traces.Health(), true)
		observed := r.Traces.HTTPHealth()
		httpSpans = map[string]any{"observed": true, "closed": observed.Closed,
			"started": strconv.FormatUint(observed.Started, 10), "ended": strconv.FormatUint(observed.Ended, 10),
			"active": strconv.FormatUint(observed.Active, 10)}
	}
	return map[string]any{"schema_version": 4, "kind": "otelc.runtime-health", "language": "go", "scope": "sdk_pipeline",
		"pid": os.Getpid(), "policy_sha256": r.Config.PolicySHA256, "selection_sha256": r.Config.SelectionSHA256,
		"phase": phase, "sdk_attached": r.Metrics != nil, "source_complete": false, "controls": r.Status(),
		"lifecycle":        map[string]string{"flush": flush, "shutdown": shutdown},
		"metric_flush":     map[string]string{"attempts": strconv.FormatUint(r.flushAttempts.Load(), 10), "failed_attempts": strconv.FormatUint(r.flushFailures.Load(), 10)},
		"probe_accounting": probeHealth(observers), "metric_exports": metrics, "trace_exports": traces,
		"http_spans":         httpSpans,
		"downstream_receipt": "unavailable", "otlp_partial_rejection": "unavailable"}
}

func probeHealth(h probe.Health) map[string]any {
	values := map[string]uint64{"function_started": h.FunctionStarts, "function_ended": h.FunctionEnds, "function_active": h.FunctionActive, "function_lost": h.FunctionLoss,
		"observer_started": h.ObserverStarts, "observer_ended": h.ObserverEnds, "observer_active": h.ObserverActive, "observer_lost": h.ObserverLoss,
		"unwind_unknown": h.UnwindUnknown, "object_admitted": h.ObjectsObserved, "gc_observed": h.GCObserved, "resource_released": h.Released,
		"objects_pending": h.LiveObjects, "resources_unreleased": h.OpenResources, "lifetime_lost": h.LifetimeLoss, "duplicate_objects": h.DuplicateObjects}
	result := map[string]any{"closed": h.Closed}
	for name, value := range values {
		result[name] = strconv.FormatUint(value, 10)
	}
	return result
}

func exportHealth(h pipeline.Health, traces bool) map[string]any {
	result := map[string]any{"observed": true, "attempts": strconv.FormatUint(h.Attempts, 10),
		"succeeded": strconv.FormatUint(h.Succeeded, 10), "failed": strconv.FormatUint(h.Failed, 10), "closed": h.Closed}
	if traces {
		result["exported_spans"] = strconv.FormatUint(h.Exported, 10)
		result["queue_pending"] = strconv.FormatUint(h.Pending, 10)
		result["queue_dropped"] = strconv.FormatUint(h.Dropped, 10)
	}
	return result
}

func outcome(ctx context.Context, err error) string {
	if err == nil {
		return "succeeded"
	}
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(ctx.Err(), context.DeadlineExceeded) {
		return "timed_out"
	}
	return "failed"
}

// Close stops controls/cleanup observation, attempts bounded final drain and
// shutdown, and atomically retains the final checkpoint. Open lifetimes are not
// completed at exit. Repeated callers see the same result.
func (r *Runtime) Close() error {
	r.closeOnce.Do(func() {
		if r.server != nil {
			r.server.close()
		}
		r.Probes.Close()
		ctx, cancel := context.WithTimeout(context.Background(), time.Duration(r.Config.ShutdownTimeoutMS)*time.Millisecond)
		defer cancel()
		r.mu.Lock()
		r.flush = "pending"
		r.shutdown = "pending"
		r.mu.Unlock()
		flushErr := r.Flush(ctx)
		shutdownErr := r.Metrics.Shutdown(ctx)
		if r.provider != nil {
			shutdownErr = errors.Join(shutdownErr, r.provider.Shutdown(ctx))
		}
		r.mu.Lock()
		r.phase = "final"
		r.flush = outcome(ctx, flushErr)
		r.shutdown = outcome(ctx, shutdownErr)
		r.mu.Unlock()
		r.closeErr = errors.Join(flushErr, shutdownErr, replaceHealth(r.directory, r.Health()))
	})
	return r.closeErr
}

func (r *Runtime) discard() {
	if r.Probes != nil {
		r.Probes.Close()
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(r.Config.ShutdownTimeoutMS)*time.Millisecond)
	defer cancel()
	if r.Metrics != nil {
		_ = r.Metrics.Shutdown(ctx)
	}
	if r.provider != nil {
		_ = r.provider.Shutdown(ctx)
	}
}
