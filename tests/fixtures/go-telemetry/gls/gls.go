// Package gls supplies a deterministic single-goroutine TLS endpoint.
package gls

var Current any

func GetTraceContextFromGLS() any { return Current }
