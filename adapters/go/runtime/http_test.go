package runtime

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"go.opentelemetry.io/otel"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

func TestLibraryTracerProviderIsOwnedOrNoopWithoutChangingGlobals(t *testing.T) {
	before := otel.GetTracerProvider()
	r := &Runtime{}
	_, span := r.TracerProvider().Tracer("http-test").Start(context.Background(), "disabled")
	if span.IsRecording() {
		t.Fatal("disabled runtime created a recording provider")
	}
	r.provider = sdktrace.NewTracerProvider()
	defer r.provider.Shutdown(context.Background())
	if r.TracerProvider() != r.provider || otel.GetTracerProvider() != before {
		t.Fatal("library instrumentation replaced or escaped its owned provider")
	}
}

func TestHTTPContextKeepsNonrecordingSDKIdentityWithoutAnExporter(t *testing.T) {
	collector := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	defer collector.Close()
	config := configuration(collector.URL)
	config.TracesEnabled = false
	config.HTTPContext = true
	r := startRuntime(t, config)
	_, span := r.TracerProvider().Tracer("http-test").Start(context.Background(), "not recorded")
	if span.IsRecording() || !span.SpanContext().IsValid() || r.Traces != nil {
		t.Fatal("disabled recording lost context or created an exporter")
	}
	span.End()
	if err := r.Close(); err != nil {
		t.Fatal(err)
	}
	if observed := r.Health()["http_spans"].(map[string]any); observed["observed"] != false || len(observed) != 1 {
		t.Fatal("unrecorded HTTP context invented observer counts", observed)
	}
}
