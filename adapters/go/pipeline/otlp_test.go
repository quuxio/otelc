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
			t.Run(signal+"/"+mode, func(t *testing.T) {
				var requests atomic.Int64
				var clean atomic.Bool
				clean.Store(mode == "clean")
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					requests.Add(1)
					if r.Method != "POST" || r.URL.Path != "/custom/"+signal || r.Header.Get("X-Fixture") != "yes" || r.Header.Get("Content-Type") != "application/x-protobuf" {
						t.Error("request does not match resolved configuration", r.Method, r.URL.Path, r.Header)
					}
					raw, err := io.ReadAll(r.Body)
					check(t, err)
					if signal == "metrics" {
						var body metricproto.ExportMetricsServiceRequest
						check(t, proto.Unmarshal(raw, &body))
						if len(body.ResourceMetrics) != 1 || body.ResourceMetrics[0].ScopeMetrics[0].Metrics[0].GetSum().DataPoints[0].GetAsInt() != 7 {
							t.Error("metric payload changed", &body)
						}
					} else {
						var body traceproto.ExportTraceServiceRequest
						check(t, proto.Unmarshal(raw, &body))
						if len(body.ResourceSpans) != 1 || body.ResourceSpans[0].ScopeSpans[0].Spans[0].Name != "workload" {
							t.Error("trace payload changed", &body)
						}
					}
					w.Header().Set("Content-Type", "application/x-protobuf")
					if clean.Load() {
						return // Empty protobuf is a complete successful OTLP response.
					}
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
						var response proto.Message
						count, message := int64(1), ""
						if mode == "warning" {
							count, message = 0, "fixture partial success warning"
						}
						if signal == "metrics" {
							response = &metricproto.ExportMetricsServiceResponse{PartialSuccess: &metricproto.ExportMetricsPartialSuccess{RejectedDataPoints: count, ErrorMessage: message}}
						} else {
							response = &traceproto.ExportTraceServiceResponse{PartialSuccess: &traceproto.ExportTracePartialSuccess{RejectedSpans: count, ErrorMessage: message}}
						}
						body, err := proto.Marshal(response)
						check(t, err)
						_, _ = w.Write(body)
					}
				}))
				defer server.Close()
				options := HTTPOptions{Endpoint: server.URL + "/custom/" + signal, Headers: map[string]string{"X-Fixture": "yes"}, Timeout: time.Second}
				var flush, shutdown func(context.Context) error
				var health func() Health
				ctx := context.Background()
				if signal == "metrics" {
					wire, err := NewHTTPMetrics(ctx, options)
					check(t, err)
					p, err := NewMetrics(metriccontrol.New(true, true), wire, resource.Empty(), 0, time.Second)
					check(t, err)
					counter, err := p.Provider.Meter(Scope).Int64Counter("calls")
					check(t, err)
					counter.Add(ctx, 7)
					flush, shutdown, health = p.Flush, p.Shutdown, p.Health
				} else {
					wire, err := NewHTTPTraces(ctx, options)
					check(t, err)
					p, err := NewTraces(wire, 8, 2, 0, time.Second)
					check(t, err)
					provider := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(p), sdktrace.WithoutPanicRecording())
					_, span := provider.Tracer(Scope).Start(ctx, "workload")
					span.End()
					flush, shutdown, health = p.ForceFlush, provider.Shutdown, p.Health
				}
				err := flush(ctx)
				if (err == nil) != (mode == "clean") || requests.Load() != 1 {
					t.Fatal("acknowledgement or retry mismatch", err, requests.Load())
				}
				if mode != "clean" {
					if h := health(); h.Failed != 1 || h.Succeeded != 0 || signal == "traces" && h.Pending != 1 {
						t.Fatal(h)
					}
					clean.Store(true)
					check(t, flush(ctx))
					if requests.Load() != 2 || health().Succeeded != 1 {
						t.Fatal("failed payload was not retried", requests.Load(), health())
					}
				}
				check(t, shutdown(ctx))
			})
		}
	}
	if globalErrors.Load() != 0 {
		t.Fatal("OTLP failure reached application handler", globalErrors.Load())
	}
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
