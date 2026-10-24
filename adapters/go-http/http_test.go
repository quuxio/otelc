package httptrace

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/codes"
	metricnoop "go.opentelemetry.io/otel/metric/noop"
	"go.opentelemetry.io/otel/propagation"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	"go.opentelemetry.io/otel/trace/noop"
	"io.quux.otelc/go/pipeline"
)

func fixture(t *testing.T) (*Instrumentation, *tracetest.InMemoryExporter, *[]trace.SpanContext) {
	t.Helper()
	exporter := tracetest.NewInMemoryExporter()
	provider := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exporter), sdktrace.WithoutPanicRecording())
	t.Cleanup(func() { provider.Shutdown(context.Background()); configured.Store(nil) })
	detached := []trace.SpanContext{}
	value, err := New(provider, metricnoop.NewMeterProvider(), func() trace.Span { return nil },
		func(span trace.Span) bool { detached = append(detached, span.SpanContext()); return true })
	if err != nil {
		t.Fatal(err)
	}
	return value, exporter, &detached
}

func response(body io.ReadCloser) *http.Response {
	return &http.Response{StatusCode: 200, Header: make(http.Header), Body: body}
}

func TestOfficialTransportClonesHeadersAndEndsOnlyOnBodyCompletion(t *testing.T) {
	value, exported, detached := fixture(t)
	request := httptest.NewRequest("GET", "http://example.test/", nil)
	base := roundTripFunc(func(next *http.Request) (*http.Response, error) {
		if next == request || next.Header.Get("traceparent") == "" || next.Context().Value(clientKey{}) != true {
			t.Fatal("request was not cloned, propagated and guarded")
		}
		return response(io.NopCloser(bytes.NewBufferString("ok"))), nil
	})
	result, err := value.RoundTrip(base, request)
	if err != nil || request.Header.Get("traceparent") != "" || len(exported.GetSpans()) != 0 || len(*detached) != 1 {
		t.Fatal("original header changed or response span ended before body completion", err)
	}
	body, err := io.ReadAll(result.Body)
	if err != nil || string(body) != "ok" || len(exported.GetSpans()) != 1 {
		t.Fatal("body completion or value differs", err)
	}
	result.Body.Close()
	if len(exported.GetSpans()) != 1 {
		t.Fatal("duplicate body completion")
	}
}

type applicationError struct{ formats int }

func (e *applicationError) Error() string { e.formats++; panic("application formatter must not run") }

type errorBody struct{ failure *applicationError }

func (b errorBody) Read([]byte) (int, error)  { return 0, b.failure }
func (b errorBody) Write([]byte) (int, error) { return 0, b.failure }
func (b errorBody) Close() error              { return b.failure }

func TestTransportAndBodyErrorsKeepIdentityWithoutCallingApplicationFormatters(t *testing.T) {
	value, exported, _ := fixture(t)
	failure := &applicationError{}
	request := httptest.NewRequest("GET", "http://example.test/", nil)
	_, err := value.RoundTrip(roundTripFunc(func(*http.Request) (*http.Response, error) { return nil, failure }), request)
	if err != failure || failure.formats != 0 || exported.GetSpans()[0].Status.Code != codes.Error || exported.GetSpans()[0].Status.Description != "" {
		t.Fatal("transport error changed or executed its formatter")
	}
	result, err := value.RoundTrip(roundTripFunc(func(*http.Request) (*http.Response, error) {
		return response(errorBody{failure}), nil
	}), request)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = result.Body.Read(make([]byte, 1)); err != failure {
		t.Fatal("read error identity changed")
	}
	if _, err = result.Body.(io.Writer).Write([]byte("x")); err != failure {
		t.Fatal("upgraded stream write error identity changed")
	}
	if err = result.Body.Close(); err != failure || failure.formats != 0 || len(exported.GetSpans()) != 2 {
		t.Fatal("close result, error formatting or span completion differs")
	}
}

func TestOriginalTransportPanicsPropagateWithoutRecoveryOrFormatting(t *testing.T) {
	value, _, _ := fixture(t)
	failure := &applicationError{}
	defer func() {
		if recover() != failure || failure.formats != 0 {
			t.Fatal("original panic changed")
		}
	}()
	value.RoundTrip(roundTripFunc(func(*http.Request) (*http.Response, error) { panic(failure) }),
		httptest.NewRequest("GET", "http://example.test/", nil))
}

type basicWriter struct {
	headers  http.Header
	statuses []int
	body     bytes.Buffer
}

func (w *basicWriter) Header() http.Header            { return w.headers }
func (w *basicWriter) WriteHeader(code int)           { w.statuses = append(w.statuses, code) }
func (w *basicWriter) Write(data []byte) (int, error) { return w.body.Write(data) }

func TestServerPreservesWriterInterfacesInformationalHeadersAndClientContext(t *testing.T) {
	value, exported, _ := fixture(t)
	writer := &basicWriter{headers: make(http.Header)}
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if _, ok := w.(http.Flusher); ok {
			t.Fatal("invented Flusher")
		}
		if _, ok := w.(http.Hijacker); ok {
			t.Fatal("invented Hijacker")
		}
		if _, ok := w.(http.Pusher); ok {
			t.Fatal("invented Pusher")
		}
		if r.Context().Value(serverKey{}) != true {
			t.Fatal("missing server recursion guard")
		}
		for _, code := range []int{103, 200, 201} {
			w.WriteHeader(code)
		}
		_, err := value.RoundTrip(roundTripFunc(func(next *http.Request) (*http.Response, error) {
			if next.Header.Get("traceparent") == "" {
				t.Fatal("server suppressed its outgoing client")
			}
			return response(http.NoBody), nil
		}), r)
		if err != nil {
			t.Fatal(err)
		}
		io.WriteString(w, "ok")
	})
	value.ServeHTTP(handler, writer, httptest.NewRequest("GET", "http://example.test/", nil))
	if len(writer.statuses) != 3 || writer.statuses[0] != 103 || writer.statuses[1] != 200 || writer.statuses[2] != 201 || writer.body.String() != "ok" {
		t.Fatal("native header forwarding or body changed", writer.statuses)
	}
	spans := exported.GetSpans()
	if len(spans) != 1 || spans[0].SpanKind != trace.SpanKindServer {
		t.Fatal("server span completion differs; abandoned client body must remain pending")
	}
}

func TestOwnedRegistrySuppressionAndDirectNativeDispatch(t *testing.T) {
	value, _, _ := fixture(t)
	if Configure(nil, value.meters, value.parent, value.detach) == nil {
		t.Fatal("nil provider accepted")
	}
	if _, err := New(value.traces, nil, value.parent, value.detach); err == nil {
		t.Fatal("nil meter accepted")
	}
	if _, err := New(value.traces, value.meters, nil, value.detach); err == nil {
		t.Fatal("nil context accepted")
	}
	if _, err := New(value.traces, value.meters, value.parent, nil); err == nil {
		t.Fatal("nil detach accepted")
	}
	before := otel.GetTracerProvider()
	if err := Configure(value.traces, value.meters, value.parent, value.detach); err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest("GET", "http://example.test/", nil)
	if !ClientEnabled(request) || !ServerEnabled(request) || ClientEnabled(nil) || ServerEnabled(nil) {
		t.Fatal("registry admission differs")
	}
	server := request.WithContext(context.WithValue(request.Context(), serverKey{}, true))
	if ServerEnabled(server) || !ClientEnabled(server) {
		t.Fatal("server marker suppressed its client")
	}
	client := request.WithContext(context.WithValue(request.Context(), clientKey{}, true))
	if ClientEnabled(client) || !ServerEnabled(client) {
		t.Fatal("client marker suppressed server")
	}
	suppressed := request.WithContext(pipeline.WithoutHTTPInstrumentation(request.Context()))
	if ClientEnabled(suppressed) || ServerEnabled(suppressed) {
		t.Fatal("SDK exporter was instrumented")
	}
	calls := 0
	base := roundTripFunc(func(*http.Request) (*http.Response, error) { calls++; return response(http.NoBody), nil })
	for _, r := range []*http.Request{nil, client, suppressed} {
		value.RoundTrip(base, r)
	}
	for _, r := range []*http.Request{nil, server, suppressed} {
		value.ServeHTTP(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { calls++ }), httptest.NewRecorder(), r)
	}
	if calls != 6 || otel.GetTracerProvider() != before {
		t.Fatal("suppression changed native dispatch or application globals")
	}
	local := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { io.WriteString(w, "ok") }))
	defer local.Close()
	r, _ := http.NewRequestWithContext(context.Background(), "GET", local.URL, nil)
	transport := &http.Transport{}
	defer transport.CloseIdleConnections()
	result, err := RoundTrip(transport, r)
	if err != nil {
		t.Fatal(err)
	}
	io.Copy(io.Discard, result.Body)
	result.Body.Close()
	ServeHTTP(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(204) }), httptest.NewRecorder(), request)
}

func TestNoopProviderNeverDetachesExistingParentAndExplicitParentIsPropagated(t *testing.T) {
	value, _, detached := fixture(t)
	_, parent := value.traces.Tracer("test").Start(context.Background(), "parent")
	defer parent.End()
	value.parent = func() trace.Span { return parent }
	for _, provider := range []trace.TracerProvider{noop.NewTracerProvider(), value.traces} {
		value.traces = provider
		result, err := value.RoundTrip(roundTripFunc(func(r *http.Request) (*http.Response, error) {
			ctx := propagation.TraceContext{}.Extract(context.Background(), propagation.HeaderCarrier(r.Header))
			if trace.SpanContextFromContext(ctx).TraceID() != parent.SpanContext().TraceID() {
				t.Fatal("implicit parent lost")
			}
			return response(http.NoBody), nil
		}), httptest.NewRequest("GET", "http://example.test/", nil))
		if err != nil {
			t.Fatal(err)
		}
		result.Body.Close()
	}
	if len(*detached) != 1 {
		t.Fatal("noop detached the existing parent", len(*detached))
	}
}
