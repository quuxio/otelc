package httptrace

import (
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"go.opentelemetry.io/otel/metric/noop"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

func TestReturnedRequestContextDoesNotSuppressTheNextClientOperation(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		_, _ = io.WriteString(writer, "reply")
	}))
	defer server.Close()
	exporter := tracetest.NewInMemoryExporter()
	provider := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exporter))
	defer func() { _ = provider.Shutdown(t.Context()) }()
	// This ordinary component provider has no implicit compiler context to detach.
	value, err := New(provider, noop.NewMeterProvider(), func() trace.Span { return nil }, func(trace.Span) bool { return false })
	if err != nil {
		t.Fatal(err)
	}
	transport := &http.Transport{}
	defer transport.CloseIdleConnections()
	request, err := http.NewRequestWithContext(t.Context(), http.MethodGet, server.URL+"/first", nil)
	if err != nil {
		t.Fatal(err)
	}
	first, err := value.RoundTrip(transport, request)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, first.Body)
	if err := first.Body.Close(); err != nil {
		t.Fatal(err)
	}
	// Reusing the context exposed by an ordinary Response.Request must not keep
	// a private recursion guard enabled after the first native call has returned.
	request, err = http.NewRequestWithContext(first.Request.Context(), http.MethodGet, server.URL+"/second", nil)
	if err != nil {
		t.Fatal(err)
	}
	second, err := value.RoundTrip(transport, request)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, second.Body)
	if err := second.Body.Close(); err != nil {
		t.Fatal(err)
	}
	if got := len(exporter.GetSpans()); got != 2 {
		t.Fatalf("two completed client operations require two spans; got %d", got)
	}
}
