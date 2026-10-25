package bridge

import "go.opentelemetry.io/otel/trace"

var Detach func(trace.Span) bool

func RegisterDetachSpanFunc(callback func(trace.Span) bool) { Detach = callback }
