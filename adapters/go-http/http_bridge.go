//go:build ignore

// Generated native bridge for the pinned Go compiler. See otelc.yaml.
package http

import _ "unsafe"

//go:linkname otelcClientEnabled io.quux.otelc/go-http.ClientEnabled
func otelcClientEnabled(*Request) bool

//go:linkname otelcServerEnabled io.quux.otelc/go-http.ServerEnabled
func otelcServerEnabled(*Request) bool

//go:linkname otelcRoundTrip io.quux.otelc/go-http.RoundTrip
func otelcRoundTrip(*Transport, *Request) (*Response, error)

//go:linkname otelcServeHTTP io.quux.otelc/go-http.ServeHTTP
func otelcServeHTTP(Handler, ResponseWriter, *Request)
