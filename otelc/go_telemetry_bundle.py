"""Extend pinned Go context frames with immutable telemetry-only metadata."""
from __future__ import annotations

TRACE_OUTPUT = 'instrumentation/go.opentelemetry.io/otel/sdk/trace/'

BRIDGE = b'''// Generated telemetry-only context bridge; see NOTICE.upstream.
package runtime

import "context"

var inheritTelemetry = func(ctx context.Context) context.Context { return ctx }

func RegisterImplicitTelemetry(f func(context.Context) context.Context) { inheritTelemetry = f }
func ContextWithImplicitTelemetry(ctx context.Context) context.Context { return inheritTelemetry(ctx) }
'''

# The public baggage API deliberately treats absence and explicit clearing alike.
# Read the pinned official context key in its own package to preserve clearing.
BAGGAGE = b'''//go:build ignore

// Generated context-presence query; see NOTICE.upstream.
package baggage

import "context"

func OtelcContextHasBaggage(ctx context.Context) bool {
 _, present := ctx.Value(baggageKey).(baggageState)
 return present
}
'''

SOURCE = b'''//go:build ignore

// Generated telemetry-only context extension; see NOTICE.upstream.
package trace

import (
 "context"
 "runtime"
 _ "unsafe"
 "go.opentelemetry.io/otel/baggage"
 internalbaggage "go.opentelemetry.io/otel/internal/baggage"
 api "go.opentelemetry.io/otel/trace"
)

//go:linkname otelcRegisterImplicitTelemetry go.opentelemetry.io/otelc/pkg/runtime.RegisterImplicitTelemetry
func otelcRegisterImplicitTelemetry(func(context.Context) context.Context)

func init() { otelcRegisterImplicitTelemetry(otelcInheritTelemetry) }

// Capture immediately after SDK creation, before span processors run. Store
// neither the caller's context chain nor a recording/application span object.
func otelcCaptureTelemetry(span api.Span, ctx context.Context) {
 value := runtime.GetTraceContextFromGLS()
 if value == nil || span == nil { return }
 tc := value.(*traceContext)
 if tc.sw == nil { return }
 current, started := tc.sw.span.SpanContext(), span.SpanContext()
 if current.TraceID() != started.TraceID() || current.SpanID() != started.SpanID() { return }
 snapshot := api.ContextWithSpanContext(context.Background(), started)
 tc.sw.telemetry = baggage.ContextWithBaggage(snapshot, baggage.FromContext(ctx))
}

// Take one current frame so a span and its baggage cannot come from different
// frames. Explicit caller values, including cleared baggage, take precedence.
func otelcInheritTelemetry(ctx context.Context) context.Context {
 value := runtime.GetTraceContextFromGLS()
 if value == nil { return ctx }
 tc := value.(*traceContext)
 span := tc.tail()
 if span == nil { return ctx }
 snapshot := tc.sw.telemetry
 if !api.SpanContextFromContext(ctx).IsValid() {
  ctx = api.ContextWithSpanContext(ctx, span.SpanContext())
 }
 if snapshot != nil && !internalbaggage.OtelcContextHasBaggage(ctx) {
  ctx = baggage.ContextWithBaggage(ctx, baggage.FromContext(snapshot))
 }
 return ctx
}
'''

RULES = b'''
otelc_telemetry_context:
  target: go.opentelemetry.io/otel/sdk/trace
  version: "v1.47.0"
  do:
    - add_file:
        file: otelc_telemetry_context.go
        path: "go.opentelemetry.io/otelc/instrumentation/go.opentelemetry.io/otel/sdk/trace"
otelc_capture_telemetry:
  target: go.opentelemetry.io/otel/sdk/trace
  version: "v1.47.0"
  where:
    func: newSpan
    recv: "*tracer"
  do:
    - inject_code:
        raw: 'defer func(){otelcCaptureTelemetry(_unnamedRetVal0, ctx)}()'
otelc_baggage_presence:
  target: go.opentelemetry.io/otel/internal/baggage
  version: "v1.47.0"
  do:
    - add_file:
        file: otelc_baggage_context.go
        path: "go.opentelemetry.io/otelc/instrumentation/go.opentelemetry.io/otel/sdk/trace"
'''


def extend(result: dict[str, bytes]) -> None:
    """Fail closed if the pinned frame/clone shapes no longer match."""
    path = TRACE_OUTPUT + 'otel_trace_context.go'
    source = result[path]
    changes = (
        (b'ended *atomic.Bool\n}', b'ended *atomic.Bool\n\ttelemetry context.Context\n}'),
        (b'&spanWrapper{span, tc.sw, stateForSpan(span)}',
         b'&spanWrapper{span: span, prev: tc.sw, ended: stateForSpan(span)}'),
    )
    for old, new in changes:
        if source.count(old) != 1:
            raise ValueError('Go upstream telemetry frame differs from the pinned profile')
        source = source.replace(old, new)
    for old in (b'&spanWrapper{inherited, nil, &atomic.Bool{}}',
                b'&spanWrapper{last, nil, tc.sw.ended}'):
        if old in source:
            source = source.replace(old, old[:-1] + b', tc.sw.telemetry}')
            break
    else:
        raise ValueError('Go upstream telemetry Clone differs from the pinned profile')
    if b'"context"' not in source:
        source = source.replace(b'import (\n', b'import (\n\t"context"\n', 1)
    result[path] = source
    result[TRACE_OUTPUT + 'otelc.yaml'] += RULES
    result[TRACE_OUTPUT + 'otelc_telemetry_context.go'] = SOURCE
    result[TRACE_OUTPUT + 'otelc_baggage_context.go'] = BAGGAGE
    result['pkg/runtime/otelc_telemetry_context.go'] = BRIDGE
