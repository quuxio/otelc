package runtime

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	metrics "go.opentelemetry.io/proto/otlp/collector/metrics/v1"
	collector "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	"google.golang.org/protobuf/proto"
	"io.quux.otelc/go/policy"
)

func TestTraceHTTPRetriesEncodedSDKTreeWithoutChangingHeaders(t *testing.T) {
	var bodies [][]byte
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		bodies = append(bodies, body)
		if r.Header.Get("Authorization") != "Bearer private-test-value" || r.Header.Get("Content-Type") != "application/x-protobuf" {
			t.Error("SDK headers changed")
		}
		if len(bodies) < 3 {
			w.WriteHeader(503)
			return
		}
		w.WriteHeader(200)
	}))
	defer server.Close()
	t.Setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "Authorization=Bearer%20private-test-value")
	p := traceTestPlan(server.URL)
	p.TraceExport.Endpoint = server.URL
	rt := &Runtime{plan: p}
	exporter, err := newTraceExporter(rt)
	if err != nil {
		t.Fatal(err)
	}
	store, err := newTraceStore(rt, nil, exporter)
	if err != nil {
		t.Fatal(err)
	}
	defer store.close(context.Background())
	root := store.begin(nil, "root", time.Now())
	store.finish(root, time.Now(), false)
	<-store.flush()
	if len(bodies) != 3 || !bytes.Equal(bodies[0], bodies[1]) || !bytes.Equal(bodies[1], bodies[2]) || rt.exportLoss.Load() != 0 {
		t.Fatal("retry changed payload or failed", len(bodies))
	}
	request := new(collector.ExportTraceServiceRequest)
	if proto.Unmarshal(bodies[0], request) != nil || len(storedSpans([]*collector.ExportTraceServiceRequest{request})) != 1 {
		t.Fatal("not a real SDK tree")
	}
}

func TestTraceHTTPAcknowledgementFailuresAreTerminal(t *testing.T) {
	partial, _ := proto.Marshal(&collector.ExportTraceServiceResponse{PartialSuccess: &collector.ExportTracePartialSuccess{RejectedSpans: 1, ErrorMessage: "private receiver error"}})
	for _, test := range []struct {
		name     string
		status   int
		body     []byte
		attempts int
	}{
		{"accepted", 202, nil, 1}, {"empty", 204, nil, 1}, {"redirect", 302, nil, 1}, {"unauthorised", 401, nil, 1},
		{"partial", 200, partial, 1}, {"malformed", 200, []byte{0xff}, 1}, {"oversized", 200, bytes.Repeat([]byte{0}, 65537), 1}, {"transient", 503, nil, 3},
	} {
		t.Run(test.name, func(t *testing.T) {
			attempts := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				attempts++
				w.WriteHeader(test.status)
				w.Write(test.body)
			}))
			defer server.Close()
			transport := &traceTransport{base: &http.Transport{}}
			defer transport.close()
			request, _ := http.NewRequest(http.MethodPost, server.URL, bytes.NewReader([]byte{1}))
			_, err := transport.RoundTrip(request)
			if err == nil || strings.Contains(err.Error(), "private receiver") || attempts != test.attempts {
				t.Fatal("acknowledgement accepted, disclosed or retried", err, attempts)
			}
		})
	}
}

func TestTraceRejectedStatusDoesNotReadAStalledBody(t *testing.T) {
	started, release := make(chan struct{}), make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(401)
		w.(http.Flusher).Flush()
		close(started)
		select {
		case <-r.Context().Done():
		case <-release:
		}
	}))
	defer server.Close()
	defer close(release)
	transport := &traceTransport{base: &http.Transport{}}
	defer transport.close()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, server.URL, bytes.NewReader([]byte{1}))
	result := make(chan error, 1)
	go func() { _, err := transport.RoundTrip(req); result <- err }()
	<-started
	select {
	case err := <-result:
		if err == nil {
			t.Fatal("rejection accepted")
		}
	case <-time.After(time.Second):
		t.Fatal("stalled rejected body consumed deadline")
	}
}

func TestTraceShutdownCancelsAnExistingLongPhase(t *testing.T) {
	started, release := make(chan struct{}), make(chan struct{})
	var once sync.Once
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/traces" {
			once.Do(func() { close(started) })
			select {
			case <-r.Context().Done():
			case <-release:
			}
			return
		}
		w.WriteHeader(200)
	}))
	defer server.Close()
	defer close(release)
	p := traceTestPlan(server.URL + "/v1/metrics")
	p.TraceExport.Endpoint = server.URL + "/v1/traces"
	p.TraceExport.TimeoutMS = 60000
	p.Runtime.ShutdownMS = 150
	rt, err := New(p)
	if err != nil {
		t.Fatal(err)
	}
	active.Store(rt)
	defer active.Store(nil)
	FinishTrace(StartTrace("root"))
	done := rt.traces.flush()
	<-started
	begin := time.Now()
	rt.Close()
	if time.Since(begin) > time.Second || rt.finished {
		t.Fatal("shutdown exceeded budget or claimed completion")
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("existing trace phase was not cancelled")
	}
	rt.traces.mu.Lock()
	defer rt.traces.mu.Unlock()
	if rt.traces.retained != 0 || len(rt.traces.ready) != 0 || rt.exportLoss.Load() == 0 {
		t.Fatal("shutdown retained trees or hid loss")
	}
}

func TestFinalTraceRejectionReachesAcknowledgedMetricHealthWithoutMoreCalls(t *testing.T) {
	requests := make(chan *metrics.ExportMetricsServiceRequest, 8)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/traces" {
			w.WriteHeader(401)
			return
		}
		body, _ := io.ReadAll(r.Body)
		request := new(metrics.ExportMetricsServiceRequest)
		if err := proto.Unmarshal(body, request); err != nil {
			t.Error(err)
		}
		requests <- request
		w.WriteHeader(200)
	}))
	defer server.Close()
	p := traceTestPlan(server.URL + "/v1/metrics")
	p.TraceExport.Endpoint = server.URL + "/v1/traces"
	rt, err := New(p)
	if err != nil {
		t.Fatal(err)
	}
	active.Store(rt)
	defer active.Store(nil)
	FinishTrace(StartTrace("root"))
	rt.Close()
	close(requests)
	health := int64(0)
	for request := range requests {
		for _, res := range request.ResourceMetrics {
			for _, scope := range res.ScopeMetrics {
				for _, metric := range scope.Metrics {
					if metric.Name == "otelc.export.dropped_batches" && metric.GetSum() != nil {
						for _, point := range metric.GetSum().DataPoints {
							health = max(health, point.GetAsInt())
						}
					}
				}
			}
		}
	}
	if health != 1 || rt.exportLoss.Load() != 1 || !rt.finished {
		t.Fatalf("acknowledged health=%d local=%d finished=%v", health, rt.exportLoss.Load(), rt.finished)
	}
}

func TestResolvedTracePlanRejectsForgedBoundsAndEndpoints(t *testing.T) {
	for _, endpoint := range []string{"http://remote.example/v1/traces", "http://localhost:0", "http://localhost:65536", "http://localhost:bad", "http://localhost/v1/traces?", "http://localhost/v1/traces#", "https://user:private@host", "file:///tmp/x"} {
		p := traceTestPlan("http://127.0.0.1")
		p.TraceExport.Endpoint = endpoint
		if validateTracePlan(p) == nil {
			t.Fatal("forged endpoint accepted", endpoint)
		}
	}
	for _, change := range []func(*policy.Plan){func(p *policy.Plan) { p.Traces.MaxActive = 65537 }, func(p *policy.Plan) { p.Traces.MaxSpans = 0 }, func(p *policy.Plan) { p.Traces.MaxActive = 65536; p.Traces.MaxSpans = 65536 }, func(p *policy.Plan) { p.Export.MaxQueued = 65 }, func(p *policy.Plan) { p.TraceExport.TimeoutMS = 60001 }, func(p *policy.Plan) { p.TraceExport.Protocol = "grpc" }, func(p *policy.Plan) { p.TraceExport = nil }} {
		p := traceTestPlan("http://127.0.0.1")
		change(&p)
		if validateTracePlan(p) == nil {
			t.Fatal("forged bounds accepted")
		}
	}
}
func TestTraceHeadersAreRuntimeOnlyBoundedAndEmptySignalOverridesGeneric(t *testing.T) {
	p := traceTestPlan("http://127.0.0.1")
	rt := &Runtime{plan: p}
	t.Setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", strings.Repeat("x", 8193))
	if _, err := newTraceExporter(rt); err == nil {
		t.Fatal("oversized header accepted")
	}
	t.Setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "private-invalid-value")
	if _, err := newTraceExporter(rt); err == nil || strings.Contains(err.Error(), "private-invalid-value") {
		t.Fatal("invalid header accepted or disclosed")
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "" {
			t.Error("generic credentials survived empty signal override")
		}
		w.WriteHeader(200)
	}))
	defer server.Close()
	rt.plan.TraceExport.Endpoint = server.URL
	t.Setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=private-generic")
	t.Setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "")
	exporter, err := newTraceExporter(rt)
	if err != nil {
		t.Fatal(err)
	}
	store, err := newTraceStore(rt, nil, exporter)
	if err != nil {
		t.Fatal(err)
	}
	root := store.begin(nil, "root", time.Now())
	store.finish(root, time.Now(), false)
	<-store.flush()
	store.close(context.Background())
	transport := &traceTransport{base: &http.Transport{}}
	transport.close()
	req, _ := http.NewRequest(http.MethodPost, server.URL, bytes.NewReader([]byte{1}))
	if _, err := transport.RoundTrip(req); err == nil {
		t.Fatal("closed transport accepted export")
	}
	req, _ = http.NewRequest(http.MethodPost, server.URL, nil)
	if _, err := transport.RoundTrip(req); err == nil {
		t.Fatal("unbounded request accepted")
	}
}
func TestTracePhaseSurvivesMetricContextCancellationAndExportsHealthWhileDisabled(t *testing.T) {
	started, release := make(chan struct{}), make(chan struct{})
	var once sync.Once
	requests := make(chan *metrics.ExportMetricsServiceRequest, 8)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/traces" {
			once.Do(func() { close(started) })
			select {
			case <-release:
				w.WriteHeader(401)
			case <-r.Context().Done():
			}
			return
		}
		body, _ := io.ReadAll(r.Body)
		request := new(metrics.ExportMetricsServiceRequest)
		if err := proto.Unmarshal(body, request); err != nil {
			t.Error(err)
		}
		requests <- request
		w.WriteHeader(200)
	}))
	defer server.Close()
	p := traceTestPlan(server.URL + "/v1/metrics")
	p.TraceExport.Endpoint = server.URL + "/v1/traces"
	p.TraceExport.TimeoutMS = 5000
	rt, err := New(p)
	if err != nil {
		t.Fatal(err)
	}
	active.Store(rt)
	defer active.Store(nil)
	defer rt.Close()
	rt.enabled.Store(false)
	FinishTrace(StartTrace("disabled-metrics"))
	ctx, cancel := context.WithCancel(context.Background())
	if err := rt.provider.ForceFlush(ctx); err != nil {
		t.Fatal(err)
	}
	<-requests
	<-started
	done := rt.traces.flush()
	cancel()
	select {
	case <-done:
		t.Fatal("metric context cancelled the independent trace phase")
	default:
	}
	close(release)
	<-done
	if rt.exportLoss.Load() != 1 {
		t.Fatal("actual trace failure not recorded")
	}
	if err := rt.provider.ForceFlush(context.Background()); err != nil {
		t.Fatal(err)
	}
	request := <-requests
	var health, calls int64
	for _, res := range request.ResourceMetrics {
		for _, scope := range res.ScopeMetrics {
			for _, metric := range scope.Metrics {
				if metric.GetSum() != nil {
					for _, point := range metric.GetSum().DataPoints {
						if metric.Name == "otelc.export.dropped_batches" {
							health += point.GetAsInt()
						}
						if metric.Name == "otelc.function.calls" {
							calls += point.GetAsInt()
						}
					}
				}
			}
		}
	}
	if health != 1 || calls != 0 {
		t.Fatalf("disabled live health=%d calls=%d", health, calls)
	}
}
