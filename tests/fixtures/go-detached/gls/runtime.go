// Package runtime supplies a single current-context slot for component tests.
// Actual runtime TLS and SDK hooks are exercised by the compiler-backed tests.
package runtime

var Current any

func GetTraceContextFromGLS() any { return Current }
