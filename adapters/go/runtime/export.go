package runtime

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"

	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	sdk "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	collector "go.opentelemetry.io/proto/otlp/collector/metrics/v1"
	"google.golang.org/protobuf/proto"
)

// The SDK encodes protobuf; this transport additionally validates all ACK bodies.
type strictTransport struct{ base http.RoundTripper }

func (t strictTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	response, err := t.base.RoundTrip(req)
	if err != nil {
		return nil, err
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, 65537))
	response.Body.Close()
	if err == nil && len(data) > 65536 {
		err = fmt.Errorf("OTLP acknowledgement exceeds 64 KiB")
	}
	if err == nil && (response.StatusCode < 200 || response.StatusCode >= 300) {
		err = fmt.Errorf("OTLP HTTP status %d", response.StatusCode)
	}
	if err == nil {
		var ack collector.ExportMetricsServiceResponse
		err = proto.Unmarshal(data, &ack)
		if err == nil && ack.GetPartialSuccess().GetRejectedDataPoints() != 0 {
			err = fmt.Errorf("OTLP rejected metric points")
		}
	}
	if err != nil {
		return nil, err
	}
	response.Body = io.NopCloser(bytes.NewReader(nil))
	response.ContentLength = 0
	return response, nil
}
func headers(generic, signal string, signalPresent bool) (map[string]string, error) {
	raw := generic
	if signalPresent {
		raw = signal
	}
	result := map[string]string{}
	if raw == "" {
		return result, nil
	}
	for _, entry := range strings.Split(raw, ",") {
		name, value, ok := strings.Cut(strings.TrimSpace(entry), "=")
		if !ok {
			return nil, fmt.Errorf("invalid OTLP header")
		}
		name, err := url.PathUnescape(name)
		if err != nil {
			return nil, fmt.Errorf("invalid OTLP header")
		}
		value, err = url.PathUnescape(value)
		if err != nil {
			return nil, fmt.Errorf("invalid OTLP header")
		}
		if name == "" || strings.ContainsAny(name, " ()<>@,;:\\\"/[]?={}\t\r\n") || strings.ContainsAny(value, "\r\n\x00") {
			return nil, fmt.Errorf("invalid OTLP header")
		}
		result[name] = value
	}
	return result, nil
}

type exporter struct {
	sdk.Exporter
	rt         *Runtime
	transport  *http.Transport
	mu         sync.Mutex
	previous   uint64
	successful bool
}

func (e *exporter) Shutdown(ctx context.Context) error {
	err := e.Exporter.Shutdown(ctx)
	e.transport.CloseIdleConnections()
	return err
}

func (e *exporter) Export(ctx context.Context, data *metricdata.ResourceMetrics) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	revision := e.rt.revision.Load()
	if e.successful && revision == e.previous {
		return nil
	}
	err := e.Exporter.Export(ctx, data)
	if err != nil {
		e.rt.exportLoss.Add(1)
	} else {
		e.previous = revision
		e.successful = true
	}
	return err
}
func newExporter(rt *Runtime) (*exporter, error) {
	signal, present := os.LookupEnv("OTEL_EXPORTER_OTLP_METRICS_HEADERS")
	values, err := headers(os.Getenv("OTEL_EXPORTER_OTLP_HEADERS"), signal, present)
	if err != nil {
		return nil, err
	}
	timeout := time.Duration(min(rt.plan.Export.TimeoutMS, rt.plan.Runtime.ShutdownMS)) * time.Millisecond
	transport := &http.Transport{Proxy: http.ProxyFromEnvironment, MaxIdleConns: 1, MaxIdleConnsPerHost: 1, IdleConnTimeout: 30 * time.Second, TLSHandshakeTimeout: timeout}
	client := &http.Client{Timeout: timeout, Transport: strictTransport{transport}, CheckRedirect: func(*http.Request, []*http.Request) error { return fmt.Errorf("OTLP redirects are unsupported") }}
	inner, err := otlpmetrichttp.New(context.Background(), otlpmetrichttp.WithEndpointURL(rt.plan.Endpoint), otlpmetrichttp.WithHeaders(values), otlpmetrichttp.WithHTTPClient(client), otlpmetrichttp.WithMaxResponseSize(65536), otlpmetrichttp.WithRetry(otlpmetrichttp.RetryConfig{Enabled: false}))
	if err != nil {
		return nil, err
	}
	return &exporter{Exporter: inner, rt: rt, transport: transport}, nil
}
