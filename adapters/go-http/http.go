// Package httptrace connects official HTTP instrumentation to the owned SDK.
package httptrace

import (
	"context"
	"errors"
	"net/http"
	"sync/atomic"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
	"io.quux.otelc/go/pipeline"
)

type clientKey struct{}
type serverKey struct{}

// Only the immediate compiler-injected native entry consumes this token. The
// application can reuse the resulting context without inheriting suppression.
func consumeNativeEntry(ctx context.Context, key any) bool {
	value, _ := ctx.Value(key).(*atomic.Bool)
	return value != nil && value.CompareAndSwap(true, false)
}

// Instrumentation uses explicit owned providers and immutable parent context.
type Instrumentation struct {
	traces trace.TracerProvider
	meters metric.MeterProvider
	parent func(context.Context) context.Context
	detach func(trace.Span) bool
}

// New never replaces an application's global provider or propagator.
func New(traces trace.TracerProvider, meters metric.MeterProvider, parent func(context.Context) context.Context,
	detach func(trace.Span) bool) (*Instrumentation, error) {
	if traces == nil || meters == nil || parent == nil || detach == nil {
		return nil, errors.New("HTTP instrumentation requires owned providers and context callbacks")
	}
	return &Instrumentation{traces: traces, meters: meters, parent: parent, detach: detach}, nil
}

func (i *Instrumentation) options(provider trace.TracerProvider) []otelhttp.Option {
	return []otelhttp.Option{otelhttp.WithTracerProvider(provider), otelhttp.WithMeterProvider(i.meters),
		otelhttp.WithPropagators(propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{}))}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) { return f(request) }

type captureProvider struct {
	trace.TracerProvider
	started func(trace.Span)
}

func (p captureProvider) Tracer(name string, options ...trace.TracerOption) trace.Tracer {
	return captureTracer{Tracer: p.TracerProvider.Tracer(name, options...), started: p.started}
}

type captureTracer struct {
	trace.Tracer
	started func(trace.Span)
}

func (t captureTracer) Start(ctx context.Context, name string, options ...trace.SpanStartOption) (context.Context, trace.Span) {
	ctx, span := t.Tracer.Start(ctx, name, options...)
	t.started(span)
	return ctx, span
}

// RoundTrip delegates request cloning, body accounting and body completion to
// the pinned official module. The native entry consumes a single-use token.
func (i *Instrumentation) RoundTrip(base http.RoundTripper, request *http.Request) (*http.Response, error) {
	if request == nil || pipeline.HTTPInstrumentationSuppressed(request.Context()) {
		return base.RoundTrip(request)
	}
	var started trace.Span
	input := trace.SpanFromContext(request.Context()).SpanContext()
	request = request.WithContext(i.parent(request.Context()))
	parent := trace.SpanContextFromContext(request.Context())
	provider := captureProvider{TracerProvider: i.traces, started: func(span trace.Span) { started = span }}
	defer func() {
		// A noop tracer may return the caller's existing span. Detach only a
		// newly created HTTP span, never the application's parent frame.
		if started != nil {
			value := started.SpanContext()
			if value.IsValid() && !sameSpanContext(value, input) &&
				!sameSpanContext(value, parent) {
				i.detach(started)
			}
		}
	}()
	native := roundTripFunc(func(next *http.Request) (*http.Response, error) {
		guard := &atomic.Bool{}
		guard.Store(true)
		defer guard.Store(false)
		return base.RoundTrip(next.WithContext(context.WithValue(next.Context(), clientKey{}, guard)))
	})
	return otelhttp.NewTransport(native, i.options(provider)...).RoundTrip(request)
}

func sameSpanContext(left, right trace.SpanContext) bool {
	return left.TraceID() == right.TraceID() && left.SpanID() == right.SpanID()
}

// ServeHTTP preserves response-writer capabilities using the official wrapper.
// Its recursion marker is separate from the client marker, so a handler's
// outgoing requests remain instrumented when they inherit the request context.
func (i *Instrumentation) ServeHTTP(base http.Handler, writer http.ResponseWriter, request *http.Request) {
	if request == nil || pipeline.HTTPInstrumentationSuppressed(request.Context()) {
		base.ServeHTTP(writer, request)
		return
	}
	native := http.HandlerFunc(func(w http.ResponseWriter, next *http.Request) {
		guard := &atomic.Bool{}
		guard.Store(true)
		defer guard.Store(false)
		base.ServeHTTP(w, next.WithContext(context.WithValue(next.Context(), serverKey{}, guard)))
	})
	otelhttp.NewHandler(native, "HTTP server", i.options(i.traces)...).ServeHTTP(writer, request)
}

var configured atomic.Pointer[Instrumentation]

// Configure is called only from private generated helpers after SDK creation.
func Configure(traces trace.TracerProvider, meters metric.MeterProvider, parent func(context.Context) context.Context,
	detach func(trace.Span) bool) error {
	value, err := New(traces, meters, parent, detach)
	if err == nil {
		configured.Store(value)
	}
	return err
}
