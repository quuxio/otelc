package pipeline

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	metricproto "go.opentelemetry.io/proto/otlp/collector/metrics/v1"
	traceproto "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	"google.golang.org/protobuf/proto"
	"io.quux.otelc/go/metriccontrol"
)

func TestOfficialExportersRequireCompleteAcknowledgementAndRetainFailedData(t *testing.T) {
	previous := otel.GetErrorHandler()
	defer otel.SetErrorHandler(previous)
	var globalErrors atomic.Int64
	otel.SetErrorHandler(otel.ErrorHandlerFunc(func(error) { globalErrors.Add(1) }))
	for _, signal := range []string{"metrics", "traces"} {
		for _, mode := range []string{"clean", "malformed", "oversized", "rejected", "warning", "unavailable", "redirect"} {
			t.Run(signal+"/"+mode, func(t *testing.T) { exerciseHTTPAcknowledgement(t, signal, mode) })
		}
	}
	if globalErrors.Load() != 0 {
		t.Fatal("OTLP failure reached application handler", globalErrors.Load())
	}
}

func assertMetricRequest(t *testing.T, raw []byte) {
	t.Helper()
	var body metricproto.ExportMetricsServiceRequest
	if err := proto.Unmarshal(raw, &body); err != nil {
		t.Error(err)
		return
	}
	var values []int64
	for _, resource := range body.ResourceMetrics {
		for _, scope := range resource.ScopeMetrics {
			for _, metric := range scope.Metrics {
				for _, point := range metric.GetSum().GetDataPoints() {
					values = append(values, point.GetAsInt())
				}
			}
		}
	}
	if len(body.ResourceMetrics) != 1 || len(values) != 1 || values[0] != 7 {
		t.Error("metric payload changed", &body)
	}
}

func assertTraceRequest(t *testing.T, raw []byte) {
	t.Helper()
	var body traceproto.ExportTraceServiceRequest
	if err := proto.Unmarshal(raw, &body); err != nil {
		t.Error(err)
		return
	}
	var names []string
	for _, resource := range body.ResourceSpans {
		for _, scope := range resource.ScopeSpans {
			for _, span := range scope.Spans {
				names = append(names, span.Name)
			}
		}
	}
	if len(body.ResourceSpans) != 1 || len(names) != 1 || names[0] != "workload" {
		t.Error("trace payload changed", &body)
	}
}

func partialAcknowledgement(signal, mode string) proto.Message {
	count, message := int64(1), ""
	if mode == "warning" {
		count, message = 0, "fixture partial success warning"
	}
	if signal == "metrics" {
		return &metricproto.ExportMetricsServiceResponse{PartialSuccess: &metricproto.ExportMetricsPartialSuccess{RejectedDataPoints: count, ErrorMessage: message}}
	}
	return &traceproto.ExportTraceServiceResponse{PartialSuccess: &traceproto.ExportTracePartialSuccess{RejectedSpans: count, ErrorMessage: message}}
}

func writeAcknowledgement(t *testing.T, w http.ResponseWriter, signal, mode string) {
	t.Helper()
	switch mode {
	case "malformed":
		_, _ = w.Write([]byte{0xff})
	case "oversized":
		_, _ = w.Write(make([]byte, maxAckBytes+1))
	case "unavailable":
		w.WriteHeader(http.StatusServiceUnavailable)
	case "redirect":
		w.Header().Set("Location", serverlessRedirect)
		w.WriteHeader(http.StatusTemporaryRedirect)
	default:
		body, err := proto.Marshal(partialAcknowledgement(signal, mode))
		if err != nil {
			t.Error(err)
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		_, _ = w.Write(body)
	}
}

func acknowledgementServer(t *testing.T, signal, mode string, requests *atomic.Int64, clean *atomic.Bool) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		if r.Method != "POST" || r.URL.Path != "/custom/"+signal || r.Header.Get("X-Fixture") != "yes" || r.Header.Get("Content-Type") != "application/x-protobuf" {
			t.Error("request does not match resolved configuration", r.Method, r.URL.Path, r.Header)
		}
		raw, err := io.ReadAll(r.Body)
		if err != nil {
			t.Error(err)
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		if signal == "metrics" {
			assertMetricRequest(t, raw)
		} else {
			assertTraceRequest(t, raw)
		}
		w.Header().Set("Content-Type", "application/x-protobuf")
		if clean.Load() {
			return
		} // Empty protobuf acknowledges complete success.
		writeAcknowledgement(t, w, signal, mode)
	}))
}

type httpTestPipeline struct {
	flush    func(context.Context) error
	shutdown func(context.Context) error
	health   func() Health
}

func acknowledgementPipeline(t *testing.T, signal, address string) httpTestPipeline {
	t.Helper()
	options := HTTPOptions{Endpoint: address + "/custom/" + signal, Headers: map[string]string{"X-Fixture": "yes"}, Timeout: time.Second}
	ctx := context.Background()
	if signal == "metrics" {
		wire, err := NewHTTPMetrics(ctx, options)
		check(t, err)
		p, err := NewMetrics(metriccontrol.New(true, true), wire, resource.Empty(), 0, time.Second)
		check(t, err)
		counter, err := p.Provider.Meter(Scope).Int64Counter("calls")
		check(t, err)
		counter.Add(ctx, 7)
		return httpTestPipeline{p.Flush, p.Shutdown, p.Health}
	}
	wire, err := NewHTTPTraces(ctx, options)
	check(t, err)
	p, err := NewTraces(wire, 8, 2, 0, time.Second)
	check(t, err)
	provider := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(p), sdktrace.WithoutPanicRecording())
	_, span := provider.Tracer(Scope).Start(ctx, "workload")
	span.End()
	return httpTestPipeline{p.ForceFlush, provider.Shutdown, p.Health}
}

func exerciseHTTPAcknowledgement(t *testing.T, signal, mode string) {
	t.Helper()
	var requests atomic.Int64
	var clean atomic.Bool
	clean.Store(mode == "clean")
	server := acknowledgementServer(t, signal, mode, &requests, &clean)
	defer server.Close()
	pipeline := acknowledgementPipeline(t, signal, server.URL)
	ctx := context.Background()
	err := pipeline.flush(ctx)
	if (err == nil) != (mode == "clean") || requests.Load() != 1 {
		t.Fatal("acknowledgement or retry mismatch", err, requests.Load())
	}
	if mode != "clean" {
		if h := pipeline.health(); h.Failed != 1 || h.Succeeded != 0 || signal == "traces" && h.Pending != 1 {
			t.Fatal(h)
		}
		clean.Store(true)
		check(t, pipeline.flush(ctx))
		if requests.Load() != 2 || pipeline.health().Succeeded != 1 {
			t.Fatal("failed payload was not retried", requests.Load(), pipeline.health())
		}
	}
	check(t, pipeline.shutdown(ctx))
}

const serverlessRedirect = "http://127.0.0.1:1/redirect-must-not-be-followed"

func TestInvalidHTTPConfigurationRefusesBeforeSDKConstruction(t *testing.T) {
	for _, endpoint := range []string{"", ":bad", "file:///tmp/otelc", "http://", "http://user:pass@localhost/", "http://localhost/?query=1", "http://localhost/#fragment", "http://localhost/" + strings.Repeat("x", 8192)} {
		for _, newExporter := range []func(context.Context, HTTPOptions) error{
			func(ctx context.Context, o HTTPOptions) error { _, err := NewHTTPMetrics(ctx, o); return err },
			func(ctx context.Context, o HTTPOptions) error { _, err := NewHTTPTraces(ctx, o); return err },
		} {
			if err := newExporter(context.Background(), HTTPOptions{Endpoint: endpoint, Timeout: time.Second}); err == nil {
				t.Fatal("accepted endpoint", endpoint)
			}
		}
	}
	for _, headers := range []map[string]string{
		{"": "value"}, {"bad name": "value"}, {"bad\x00name": "value"}, {"name": "bad\r\nvalue"},
		{"name": "bad\x00value"}, {"name": "bad\x7fvalue"}, {"name": strings.Repeat("x", 8193)},
	} {
		if err := (HTTPOptions{Endpoint: "http://localhost/v1/metrics", Headers: headers, Timeout: time.Second}).validate(); err == nil {
			t.Fatal("accepted invalid headers")
		}
	}
	if err := (HTTPOptions{Endpoint: "http://localhost", Timeout: 0}).validate(); err == nil {
		t.Fatal("accepted missing timeout")
	}
}
