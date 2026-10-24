package pipeline

import (
	"bytes"
	"context"
	"errors"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	metricproto "go.opentelemetry.io/proto/otlp/collector/metrics/v1"
	traceproto "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	"google.golang.org/protobuf/proto"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

const maxRequestBytes = 16 * 1024 * 1024
const maxAckBytes = 64 * 1024

// HTTPOptions contains resolved signal configuration. The common policy adapter
// supplies a full signal URL and resolved headers; ambient SDK defaults must not
// replace those choices.
type HTTPOptions struct {
	Endpoint string
	Headers  map[string]string
	Timeout  time.Duration
}

func (o HTTPOptions) validate() error {
	u, err := url.Parse(o.Endpoint)
	if err != nil || len(o.Endpoint) > 8192 || u == nil || u.Host == "" || u.Hostname() == "" ||
		(u.Scheme != "http" && u.Scheme != "https") || u.User != nil || u.RawQuery != "" ||
		u.Fragment != "" || u.Opaque != "" || o.Timeout <= 0 {
		return errors.New("invalid resolved OTLP HTTP endpoint or timeout")
	}
	size := 0
	for name, value := range o.Headers {
		size += len(name) + len(value)
		if name == "" || strings.ContainsAny(name, " ()<>@,;:\\\"/[]?={}\t\r\n") {
			return errors.New("invalid resolved OTLP HTTP header")
		}
		for _, character := range []byte(name) {
			if character < 33 || character > 126 {
				return errors.New("invalid resolved OTLP HTTP header")
			}
		}
		for _, character := range []byte(value) {
			if character < 32 && character != '\t' || character == 127 {
				return errors.New("invalid resolved OTLP HTTP header")
			}
		}
	}
	if size > 8192 {
		return errors.New("resolved OTLP HTTP headers exceed budget")
	}
	return nil
}

type acknowledgementTransport struct {
	base    *http.Transport
	metrics bool
}

func (t acknowledgementTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	request = request.WithContext(WithoutHTTPInstrumentation(request.Context()))
	response, err := t.base.RoundTrip(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, errors.New("OTLP HTTP response status rejected")
	}
	raw, err := io.ReadAll(io.LimitReader(response.Body, maxAckBytes+1))
	if err != nil {
		return nil, err
	}
	if len(raw) > maxAckBytes {
		return nil, errors.New("OTLP HTTP acknowledgement exceeds budget")
	}
	if t.metrics {
		var ack metricproto.ExportMetricsServiceResponse
		if proto.Unmarshal(raw, &ack) != nil || ack.GetPartialSuccess().GetRejectedDataPoints() != 0 ||
			ack.GetPartialSuccess().GetErrorMessage() != "" {
			return nil, errors.New("OTLP metric acknowledgement rejected")
		}
	} else {
		var ack traceproto.ExportTraceServiceResponse
		if proto.Unmarshal(raw, &ack) != nil || ack.GetPartialSuccess().GetRejectedSpans() != 0 ||
			ack.GetPartialSuccess().GetErrorMessage() != "" {
			return nil, errors.New("OTLP trace acknowledgement rejected")
		}
	}
	// Some official exporter partial-success paths call global otel.Handle.
	// Validate the real protobuf acknowledgement here, then give the SDK a clean
	// empty acknowledgement. Never filter or synthesise a failed response.
	response.Body = io.NopCloser(bytes.NewReader(nil))
	response.ContentLength = 0
	return response, nil
}

func client(options HTTPOptions, metrics bool) (*http.Client, *http.Transport) {
	transport := &http.Transport{Proxy: http.ProxyFromEnvironment,
		MaxIdleConns: 1, MaxIdleConnsPerHost: 1, IdleConnTimeout: 30 * time.Second,
		TLSHandshakeTimeout: options.Timeout, ResponseHeaderTimeout: options.Timeout}
	return &http.Client{Timeout: options.Timeout,
		Transport: acknowledgementTransport{base: transport, metrics: metrics},
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return errors.New("OTLP HTTP redirects are unsupported")
		}}, transport
}

type httpMetricExporter struct {
	sdkmetric.Exporter
	transport *http.Transport
}

func (e httpMetricExporter) Shutdown(ctx context.Context) error {
	err := e.Exporter.Shutdown(ctx)
	e.transport.CloseIdleConnections()
	return err
}

// NewHTTPMetrics reuses the official protobuf encoder and HTTP exporter with
// bounded acknowledgement validation and pipeline-owned retry scheduling.
func NewHTTPMetrics(ctx context.Context, options HTTPOptions) (sdkmetric.Exporter, error) {
	if err := options.validate(); err != nil {
		return nil, err
	}
	httpClient, transport := client(options, true)
	exporter, err := otlpmetrichttp.New(ctx, otlpmetrichttp.WithEndpointURL(options.Endpoint),
		otlpmetrichttp.WithHTTPClient(httpClient), otlpmetrichttp.WithHeaders(options.Headers),
		otlpmetrichttp.WithTimeout(options.Timeout), otlpmetrichttp.WithCompression(otlpmetrichttp.NoCompression),
		otlpmetrichttp.WithRetry(otlpmetrichttp.RetryConfig{Enabled: false}),
		otlpmetrichttp.WithMaxRequestSize(maxRequestBytes), otlpmetrichttp.WithMaxResponseSize(maxAckBytes))
	if err != nil {
		transport.CloseIdleConnections()
		return nil, err
	}
	return httpMetricExporter{Exporter: exporter, transport: transport}, nil
}

type httpTraceExporter struct {
	sdktrace.SpanExporter
	transport *http.Transport
}

func (e httpTraceExporter) Shutdown(ctx context.Context) error {
	err := e.SpanExporter.Shutdown(ctx)
	e.transport.CloseIdleConnections()
	return err
}

// NewHTTPTraces uses the official trace exporter without its internal retries.
// The bounded trace queue retains batches whose acknowledgements fail validation.
func NewHTTPTraces(ctx context.Context, options HTTPOptions) (sdktrace.SpanExporter, error) {
	if err := options.validate(); err != nil {
		return nil, err
	}
	httpClient, transport := client(options, false)
	exporter, err := otlptracehttp.New(ctx, otlptracehttp.WithEndpointURL(options.Endpoint),
		otlptracehttp.WithHTTPClient(httpClient), otlptracehttp.WithHeaders(options.Headers),
		otlptracehttp.WithTimeout(options.Timeout), otlptracehttp.WithCompression(otlptracehttp.NoCompression),
		otlptracehttp.WithEncoding(otlptracehttp.EncodingProtobuf),
		otlptracehttp.WithRetry(otlptracehttp.RetryConfig{Enabled: false}),
		otlptracehttp.WithMaxRequestSize(maxRequestBytes), otlptracehttp.WithMaxResponseSize(maxAckBytes))
	if err != nil {
		transport.CloseIdleConnections()
		return nil, err
	}
	return httpTraceExporter{SpanExporter: exporter, transport: transport}, nil
}
