package runtime

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	goruntime "runtime"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/trace"
	metricproto "go.opentelemetry.io/proto/otlp/collector/metrics/v1"
	traceproto "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	"google.golang.org/protobuf/proto"
	"io.quux.otelc/go/probe"
)

type receiver struct {
	mu                    sync.Mutex
	metricRequests, spans int
	calls                 int64
	reject                bool
}

func (s *receiver) serve(w http.ResponseWriter, r *http.Request) {
	data, err := io.ReadAll(r.Body)
	if err != nil {
		w.WriteHeader(500)
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.reject {
		w.WriteHeader(503)
		return
	}
	switch r.URL.Path {
	case "/v1/metrics":
		var request metricproto.ExportMetricsServiceRequest
		if proto.Unmarshal(data, &request) != nil {
			w.WriteHeader(400)
			return
		}
		s.metricRequests++
		for _, res := range request.ResourceMetrics {
			for _, scope := range res.ScopeMetrics {
				for _, m := range scope.Metrics {
					if m.Name == "otelc.function.calls" {
						for _, p := range m.GetSum().DataPoints {
							s.calls = p.GetAsInt()
						}
					}
				}
			}
		}
	case "/v1/traces":
		var request traceproto.ExportTraceServiceRequest
		if proto.Unmarshal(data, &request) != nil {
			w.WriteHeader(400)
			return
		}
		for _, res := range request.ResourceSpans {
			for _, scope := range res.ScopeSpans {
				s.spans += len(scope.Spans)
			}
		}
	default:
		w.WriteHeader(404)
		return
	}
	w.Header().Set("Content-Type", "application/x-protobuf")
}

func (s *receiver) snapshot() (int, int, int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.metricRequests, s.spans, s.calls
}

func configuration(endpoint string) Config {
	return Config{SchemaVersion: 2, ServiceName: "go-runtime-test", Endpoint: endpoint, IntervalMS: 60000, TimeoutMS: 100, ShutdownTimeoutMS: 500,
		Collection: true, Emission: true, TracesEnabled: true, SampleRatio: 1, Functions: []string{"workload"}, Lifetimes: []probe.Lifetime{},
		MaxLive: 128, Boundaries: []float64{.001, .01}, PolicySHA256: strings.Repeat("a", 64), SelectionSHA256: strings.Repeat("b", 64)}
}

func startRuntime(t *testing.T, c Config) *Runtime {
	t.Helper()
	r, err := New(c, privateTemp(t), func(ctx context.Context) context.Context { return ctx }, func(trace.Span) bool { return true })
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = r.Close() })
	return r
}

func command(t *testing.T, r *Runtime, text string) map[string]any {
	t.Helper()
	client, err := net.DialTimeout("tcp4", r.server.listener.Addr().String(), time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	_ = client.SetDeadline(time.Now().Add(3 * time.Second))
	if err := json.NewEncoder(client).Encode(map[string]string{"token": r.server.token, "command": text}); err != nil {
		t.Fatal(err)
	}
	var result map[string]any
	if err := json.NewDecoder(client).Decode(&result); err != nil {
		t.Fatal(err)
	}
	return result
}

func invoke(r *Runtime) { defer r.Probes.Start(0).End(false) }

func TestRuntimeStartupOffAndEveryLiveStateWithIndependentTraces(t *testing.T) {
	for _, initial := range []bool{false, true} {
		t.Run(strconv.FormatBool(initial), func(t *testing.T) {
			sink := &receiver{}
			server := httptest.NewServer(http.HandlerFunc(sink.serve))
			t.Cleanup(server.Close)
			cfg := configuration(server.URL)
			cfg.Collection, cfg.Emission = initial, initial
			globalMeter, globalTracer := otel.GetMeterProvider(), otel.GetTracerProvider()
			r := startRuntime(t, cfg)
			for _, step := range []struct {
				command              string
				collection, emission bool
			}{
				{"on", true, true}, {"emission off", true, false}, {"collection off", false, false}, {"emission on", false, true}, {"collection on", true, true}, {"off", false, false},
			} {
				state := command(t, r, step.command)
				if state["collection"] != step.collection || state["emission"] != step.emission || state["pid"] != float64(os.Getpid()) {
					t.Fatal("wrong live state or PID")
				}
				metricsBefore, tracesBefore, callsBefore := sink.snapshot()
				invoke(r)
				if response := command(t, r, "flush"); response["error"] != nil {
					t.Fatal("flush refused")
				}
				metricsAfter, tracesAfter, callsAfter := sink.snapshot()
				if tracesAfter != tracesBefore+1 {
					t.Fatal("metric state suppressed traces")
				}
				if step.collection && step.emission {
					if metricsAfter != metricsBefore+1 || callsAfter <= callsBefore {
						t.Fatal("enabled metrics missing")
					}
				} else if metricsAfter != metricsBefore {
					t.Fatal("disabled metrics transmitted cached snapshot")
				}
			}
			health := command(t, r, "health")
			if health["schema_version"] != float64(4) || health["source_complete"] != false || health["downstream_receipt"] != "unavailable" {
				t.Fatal("invalid health claims")
			}
			if command(t, r, "status")["pid"] != float64(os.Getpid()) {
				t.Fatal("PID changed")
			}
			if otel.GetMeterProvider() != globalMeter || otel.GetTracerProvider() != globalTracer {
				t.Fatal("changed global providers")
			}
			if err := r.Close(); err != nil {
				t.Fatal(err)
			}
			if err := r.Close(); err != nil {
				t.Fatal(err)
			}
			var final map[string]any
			data, err := os.ReadFile(filepath.Join(r.directory, "health.json"))
			if err != nil {
				t.Fatal(err)
			}
			if json.Unmarshal(data, &final) != nil || final["phase"] != "final" {
				t.Fatal("missing final checkpoint")
			}
			if info, _ := os.Stat(filepath.Join(r.directory, "health.json")); info.Mode().Perm() != 0600 {
				t.Fatal("public health permissions")
			}
		})
	}
}

func TestRuntimeExportFailureRemainsVisibleAfterRetry(t *testing.T) {
	sink := &receiver{reject: true}
	server := httptest.NewServer(http.HandlerFunc(sink.serve))
	t.Cleanup(server.Close)
	r := startRuntime(t, configuration(server.URL))
	invoke(r)
	if command(t, r, "flush")["error"] == nil {
		t.Fatal("failed export acknowledged")
	}
	sink.mu.Lock()
	sink.reject = false
	sink.mu.Unlock()
	if command(t, r, "flush")["error"] != nil {
		t.Fatal("retry did not recover")
	}
	if r.Metrics.Health().Failed == 0 || r.Traces.Health().Failed == 0 || r.Traces.Health().Pending != 0 {
		t.Fatal("failure disappeared or queue not drained")
	}
	if _, spans, calls := sink.snapshot(); spans != 1 || calls != 1 {
		t.Fatal("retry lost observations")
	}
}

func TestRuntimeTracingOffAndUnresolvedClose(t *testing.T) {
	sink := &receiver{}
	server := httptest.NewServer(http.HandlerFunc(sink.serve))
	t.Cleanup(server.Close)
	cfg := configuration(server.URL)
	cfg.TracesEnabled = false
	cfg.Lifetimes = []probe.Lifetime{{Name: "Object", Resource: true}}
	r := startRuntime(t, cfg)
	object := new([32]byte)
	probe.Track(r.Probes, object, 0)
	invoke(r)
	if err := r.Close(); err != nil {
		t.Fatal(err)
	}
	if r.Traces != nil || r.Health()["trace_exports"].(map[string]any)["observed"] != false {
		t.Fatal("traces-off attached provider")
	}
	if _, spans, calls := sink.snapshot(); spans != 0 || calls != 1 {
		t.Fatal("traces-off changed metrics")
	}
	state := r.Probes.Snapshot()
	if state.GCObserved != 0 || state.Released != 0 || state.LiveObjects != 1 || state.OpenResources != 1 {
		t.Fatal("shutdown fabricated lifetime boundary")
	}
	goruntime.KeepAlive(object)
}

func TestRuntimeCloseWithIncompleteClientAndCancelledCommand(t *testing.T) {
	sink := &receiver{}
	server := httptest.NewServer(http.HandlerFunc(sink.serve))
	t.Cleanup(server.Close)
	r := startRuntime(t, configuration(server.URL))
	client, err := net.Dial("tcp4", r.server.listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	_, _ = io.WriteString(client, "{\"token\":")
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := r.server.run(ctx, "off"); err == nil {
		t.Fatal("cancelled command acknowledged")
	}
	start := time.Now()
	if err := r.Close(); err != nil {
		t.Fatal(err)
	}
	if time.Since(start) > time.Second {
		t.Fatal("incomplete client stalled shutdown")
	}
}

func TestControlRefusesMalformedAndUnauthenticatedRequests(t *testing.T) {
	sink := &receiver{}
	server := httptest.NewServer(http.HandlerFunc(sink.serve))
	t.Cleanup(server.Close)
	r := startRuntime(t, configuration(server.URL))
	for _, payload := range []string{"{}\n", "[]\n", "null\n", "{\"token\":\"wrong\",\"command\":\"off\"}\n", strings.Repeat("x", 4097) + "\n"} {
		client, err := net.Dial("tcp4", r.server.listener.Addr().String())
		if err != nil {
			t.Fatal(err)
		}
		_ = client.SetDeadline(time.Now().Add(time.Second))
		_, _ = io.WriteString(client, payload)
		line, _ := bufio.NewReader(client).ReadString('\n')
		client.Close()
		if line != "" {
			t.Fatal("invalid request received response")
		}
	}
	if command(t, r, "restart")["error"] == nil {
		t.Fatal("unknown command admitted")
	}
	if !r.Controls.CollectionEnabled() || !r.Controls.EmissionEnabled() {
		t.Fatal("invalid request changed state")
	}
}

func TestRuntimeConfigurationFailureAndAmbientSDKRefusal(t *testing.T) {
	for _, change := range []func(*Config){func(c *Config) { c.SchemaVersion = 1 }, func(c *Config) { c.ServiceName = "" }, func(c *Config) { c.ServiceName = "bad\n" }, func(c *Config) { c.PolicySHA256 = "bad" }, func(c *Config) { c.SampleRatio = 2 }, func(c *Config) { c.IntervalMS = 0 }, func(c *Config) { c.MaxLive = 0 }, func(c *Config) { c.Endpoint = "invalid" }} {
		cfg := configuration("http://127.0.0.1:1")
		change(&cfg)
		if r, err := New(cfg, privateTemp(t), func(ctx context.Context) context.Context { return ctx }, func(trace.Span) bool { return true }); err == nil {
			r.Close()
			t.Fatal("invalid config accepted")
		}
	}
	t.Setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "not-an-integer")
	if RejectAmbient() == nil {
		t.Fatal("ambient SDK options accepted")
	}
	if _, err := New(configuration("http://127.0.0.1:1"), privateTemp(t), func(ctx context.Context) context.Context { return ctx }, func(trace.Span) bool { return true }); err == nil {
		t.Fatal("created SDK with ambient options")
	}
}

func TestLifecycleFailureAndOutcomes(t *testing.T) {
	if outcome(context.Background(), fmt.Errorf("failure")) != "failed" || outcome(context.Background(), context.DeadlineExceeded) != "timed_out" {
		t.Fatal("bad outcomes")
	}
	sink := &receiver{reject: true}
	server := httptest.NewServer(http.HandlerFunc(sink.serve))
	t.Cleanup(server.Close)
	r := startRuntime(t, configuration(server.URL))
	invoke(r)
	if r.Close() == nil {
		t.Fatal("failed shutdown was suppressed")
	}
	health := r.Health()
	states := health["lifecycle"].(map[string]string)
	if health["phase"] != "final" || states["flush"] == "succeeded" || states["shutdown"] == "succeeded" {
		t.Fatal("shutdown falsely clean")
	}
}

func TestCollectionSwitchDoesNotWaitForAnUnrelatedMetricExport(t *testing.T) {
	seen := make(chan struct{})
	release := make(chan struct{})
	var first sync.Once
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		_, _ = io.Copy(io.Discard, request.Body)
		first.Do(func() { close(seen) })
		select {
		case <-release:
		case <-request.Context().Done():
		}
		w.Header().Set("Content-Type", "application/x-protobuf")
	}))
	t.Cleanup(server.Close)
	cfg := configuration(server.URL)
	cfg.TracesEnabled = false
	cfg.TimeoutMS = 5000
	r := startRuntime(t, cfg)
	finished := make(chan error, 1)
	invoke(r)
	go func() { finished <- r.Flush(context.Background()) }()
	<-seen
	response := command(t, r, "collection off")
	close(release)
	if err := <-finished; err != nil {
		t.Fatal(err)
	}
	if response["error"] != nil || response["collection"] != false || response["emission"] != true {
		t.Fatal("collection-only change waited for an unrelated emission barrier")
	}
}
