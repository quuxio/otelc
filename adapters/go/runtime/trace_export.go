package runtime

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"sync"
	"time"

	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	collector "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	"google.golang.org/protobuf/proto"
)

// The public SDK encodes once. Retry GetBody within the original phase context.
type traceTransport struct {
	base   *http.Transport
	mu     sync.Mutex
	cancel context.CancelFunc
	closed bool
}

func (t *traceTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	if req.Body != nil {
		defer req.Body.Close()
	}
	if req.ContentLength <= 0 || req.ContentLength > maxTraceBytes || req.GetBody == nil {
		return nil, fmt.Errorf("invalid OTLP trace request bounds")
	}
	ctx, cancel := context.WithCancel(req.Context())
	defer cancel()
	t.mu.Lock()
	if t.closed {
		t.mu.Unlock()
		return nil, fmt.Errorf("OTLP trace transport closed")
	}
	t.cancel = cancel
	t.mu.Unlock()
	defer func() { t.mu.Lock(); t.cancel = nil; t.mu.Unlock() }()
	for attempt := 0; attempt < 3; attempt++ {
		cloned := req.Clone(ctx)
		body, err := req.GetBody()
		if err != nil {
			return nil, fmt.Errorf("OTLP trace request unavailable")
		}
		cloned.Body = body
		response, err := t.base.RoundTrip(cloned)
		if err != nil {
			if ctx.Err() != nil {
				return nil, fmt.Errorf("OTLP trace deadline")
			}
			continue
		}
		if response.StatusCode != http.StatusOK {
			status := response.StatusCode
			response.Body.Close()
			if status == 429 || status == 502 || status == 503 || status == 504 {
				continue
			}
			return nil, fmt.Errorf("OTLP trace HTTP status %d", status)
		}
		ack, readErr := io.ReadAll(io.LimitReader(response.Body, 65537))
		response.Body.Close()
		if readErr != nil {
			if ctx.Err() != nil {
				return nil, fmt.Errorf("OTLP trace deadline")
			}
			continue
		}
		if len(ack) > 65536 {
			return nil, fmt.Errorf("OTLP trace acknowledgement exceeds bounds")
		}
		var result collector.ExportTraceServiceResponse
		if proto.Unmarshal(ack, &result) != nil || result.GetPartialSuccess().GetRejectedSpans() != 0 {
			return nil, fmt.Errorf("OTLP trace acknowledgement rejected")
		}
		response.Body = io.NopCloser(bytes.NewReader(nil))
		response.ContentLength = 0
		return response, nil
	}
	return nil, fmt.Errorf("OTLP trace attempts exhausted")
}
func (t *traceTransport) close() {
	t.mu.Lock()
	t.closed = true
	if t.cancel != nil {
		t.cancel()
	}
	t.mu.Unlock()
	t.base.CloseIdleConnections()
}

type traceExporter struct {
	sdktrace.SpanExporter
	transport *traceTransport
}

func (e *traceExporter) Shutdown(ctx context.Context) error {
	e.transport.close()
	return e.SpanExporter.Shutdown(ctx)
}
func newTraceExporter(rt *Runtime) (*traceExporter, error) {
	if err := validateTracePlan(rt.plan); err != nil {
		return nil, err
	}
	signal, present := os.LookupEnv("OTEL_EXPORTER_OTLP_TRACES_HEADERS")
	generic := os.Getenv("OTEL_EXPORTER_OTLP_HEADERS")
	raw := generic
	if present {
		raw = signal
	}
	if len(raw) > 8192 {
		return nil, fmt.Errorf("OTLP headers exceed 8192 bytes")
	}
	values, err := headers(generic, signal, present)
	if err != nil {
		return nil, err
	}
	timeout := time.Duration(rt.plan.TraceExport.TimeoutMS) * time.Millisecond
	transport := &traceTransport{base: &http.Transport{Proxy: http.ProxyFromEnvironment, MaxIdleConns: 1, MaxIdleConnsPerHost: 1, IdleConnTimeout: 30 * time.Second, TLSHandshakeTimeout: timeout}}
	client := &http.Client{Timeout: timeout, Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error { return fmt.Errorf("OTLP redirects are unsupported") }}
	exporter, err := otlptracehttp.New(context.Background(), otlptracehttp.WithEndpointURL(rt.plan.TraceExport.Endpoint), otlptracehttp.WithEncoding(otlptracehttp.EncodingProtobuf), otlptracehttp.WithCompression(otlptracehttp.NoCompression), otlptracehttp.WithHeaders(values), otlptracehttp.WithHTTPClient(client), otlptracehttp.WithRetry(otlptracehttp.RetryConfig{Enabled: false}), otlptracehttp.WithMaxRequestSize(maxTraceBytes), otlptracehttp.WithMaxResponseSize(65536))
	if err != nil {
		transport.close()
		return nil, fmt.Errorf("OTLP trace exporter startup failed")
	}
	return &traceExporter{SpanExporter: exporter, transport: transport}, nil
}
