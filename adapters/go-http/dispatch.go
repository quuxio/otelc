package httptrace

import (
	"net/http"

	"io.quux.otelc/go/pipeline"
)

// ClientEnabled guards the native entry before constructing an official wrapper.
func ClientEnabled(request *http.Request) bool {
	return configured.Load() != nil && request != nil && !pipeline.HTTPInstrumentationSuppressed(request.Context()) &&
		!consumeNativeEntry(request.Context(), clientKey{})
}

// ServerEnabled is independent of the client recursion marker.
func ServerEnabled(request *http.Request) bool {
	return configured.Load() != nil && request != nil && !pipeline.HTTPInstrumentationSuppressed(request.Context()) &&
		!consumeNativeEntry(request.Context(), serverKey{})
}

// RoundTrip is called by the compiler-generated native entry. It deliberately
// uses direct injected code, not a recoverable before-hook: original panics
// must propagate through the application unchanged.
func RoundTrip(transport *http.Transport, request *http.Request) (*http.Response, error) {
	return configured.Load().RoundTrip(transport, request)
}

// ServeHTTP calls the existing serverHandler through its public method set.
func ServeHTTP(handler http.Handler, writer http.ResponseWriter, request *http.Request) {
	configured.Load().ServeHTTP(handler, writer, request)
}
