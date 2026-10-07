package runtime

import (
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	collector "go.opentelemetry.io/proto/otlp/collector/metrics/v1"
	"google.golang.org/protobuf/proto"
	"io.quux.otelc/go/policy"
)

func testPlan(endpoint string) policy.Plan {
	p := policy.Plan{Language: "go", Available: true, Endpoint: endpoint}
	p.Runtime.MaxFunctions = 8
	p.Runtime.MaxActive = 32
	p.Runtime.ShutdownMS = 500
	p.Metrics.Enabled = true
	p.Metrics.Bounds = []float64{.001, .01, 1}
	p.Export.IntervalMS = 60000
	p.Export.TimeoutMS = 100
	p.Resource.Name = "go-test"
	p.Resource.Version = "1"
	p.Resource.Attributes = map[string]string{"test": "value"}
	return p
}
func TestSDKMetricsDeferAndPanicSemantics(t *testing.T) {
	var mu sync.Mutex
	requests := []*collector.ExportMetricsServiceRequest{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		data, _ := io.ReadAll(r.Body)
		var req collector.ExportMetricsServiceRequest
		if err := proto.Unmarshal(data, &req); err != nil {
			t.Error(err)
		}
		mu.Lock()
		requests = append(requests, &req)
		mu.Unlock()
		w.WriteHeader(200)
	}))
	defer server.Close()
	rt, err := New(testPlan(server.URL))
	if err != nil {
		t.Fatal(err)
	}
	active.Store(rt)
	defer active.Store(nil)
	report := filepath.Join(t.TempDir(), "report.json")
	t.Setenv("OTELC_REPORT_PATH", report)
	marker := &struct{ value string }{"same"}
	func() {
		defer func() {
			if recover() != marker {
				t.Error("panic value changed")
			}
		}()
		func() { defer Finish(Start("escaping")); panic(marker) }()
	}()
	caught := func() (value int) {
		defer Finish(Start("caught"))
		defer func() {
			if recover() != nil {
				value = 7
			}
		}()
		panic("caught")
	}
	if caught() != 7 {
		t.Fatal("direct recover changed")
	}
	func() { defer Finish(Start("normal")) }()
	func() { defer Finish(Start("normal")) }()
	var wait sync.WaitGroup
	for range 8 {
		wait.Go(func() { defer Finish(Start("workers")) })
	}
	wait.Wait()
	token := Start("inflight")
	rt.enabled.Store(false)
	if Start("inflight") != 0 {
		t.Fatal("disabled call admitted")
	}
	rt.finish(token, true)
	rt.enabled.Store(true)
	Finish(0)
	Close()
	rt.Close()
	result := rt.report()
	if result["function_calls"] != uint64(13) || result["export_loss"] != uint64(0) || result["export_finished"] != true {
		t.Fatalf("bad report %#v", result)
	}
	if rt.functions["escaping"].Unwinds != 1 || rt.functions["caught"].Unwinds != 0 {
		t.Fatal("unwind semantics")
	}
	if _, err := os.ReadFile(report); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(requests) != 1 {
		t.Fatalf("unchanged SDK snapshot sent twice: %d", len(requests))
	}
	count := int64(0)
	hist := uint64(0)
	for _, resource := range requests[0].ResourceMetrics {
		for _, scope := range resource.ScopeMetrics {
			for _, metric := range scope.Metrics {
				if metric.Name == "otelc.function.calls" {
					for _, point := range metric.GetSum().DataPoints {
						count += point.GetAsInt()
					}
				}
				if metric.Name == "otelc.function.duration" {
					for _, point := range metric.GetHistogram().DataPoints {
						hist += point.Count
						if point.Sum == nil || *point.Sum < 0 {
							t.Error("invalid seconds histogram")
						}
					}
				}
			}
		}
	}
	if count != 13 || hist != 13 {
		t.Fatalf("SDK protobuf counts: %d %d", count, hist)
	}
	active.Store(nil)
	if Start("none") != 0 {
		t.Fatal("unmanaged call admitted")
	}
	Finish(1)
}
func TestLimitsPrivateControlsAndLegacyNilPanic(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) }))
	defer server.Close()
	dir, err := os.MkdirTemp("", "go-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	os.Chmod(dir, 0700)
	socket := filepath.Join(dir, "metrics.sock")
	p := testPlan(server.URL)
	p.Runtime.Socket = &socket
	p.Runtime.MaxFunctions = 1
	p.Runtime.MaxActive = 1
	rt, err := New(p)
	if err != nil {
		t.Fatal(err)
	}
	active.Store(rt)
	defer active.Store(nil)
	controlRequest := func(value string) map[string]any {
		connection, err := net.Dial("unix", socket)
		if err != nil {
			t.Fatal(err)
		}
		defer connection.Close()
		connection.SetDeadline(time.Now().Add(time.Second))
		io.WriteString(connection, value)
		var response map[string]any
		if err = json.NewDecoder(connection).Decode(&response); err != nil {
			t.Fatal(err)
		}
		return response
	}
	info, _ := os.Stat(socket)
	if info.Mode().Perm() != 0600 {
		t.Fatal("public control socket")
	}
	if controlRequest("status\n")["pid"] != float64(os.Getpid()) {
		t.Fatal("wrong PID")
	}
	token := Start("one")
	if Start("one") != 0 || Start("two") != 0 {
		t.Fatal("capacity not enforced")
	}
	rt.finish(token, false)
	if controlRequest("disable\n")["metrics_enabled"] != false || Start("one") != 0 {
		t.Fatal("disable failed")
	}
	if controlRequest("enable\n")["metrics_enabled"] != true {
		t.Fatal("enable failed")
	}
	for _, value := range []string{"unknown\n", "xxxxxxxxxxxxxxxxxxxx", "unfinished"} {
		if controlRequest(value)["error"] == nil {
			t.Fatal("invalid control accepted")
		}
	}
	t.Setenv("GODEBUG", "panicnil=1")
	func() { defer Finish(Start("one")) }()
	t.Setenv("GODEBUG", "panicnil=0")
	Start("one")
	rt.Close()
	if rt.losses["active_call_capacity"] != 1 || rt.losses["function_capacity"] != 1 || rt.losses["unsupported_runtime"] != 1 || rt.losses["incomplete"] != 1 {
		t.Fatalf("incorrect loss accounting %#v", rt.losses)
	}
	if _, err = os.Stat(socket); !os.IsNotExist(err) {
		t.Fatal("owned control socket leaked")
	}
	os.WriteFile(socket, []byte("occupied"), 0600)
	if _, err = New(p); err == nil {
		t.Fatal("occupied socket overwritten")
	}
	os.Remove(socket)
	os.Chmod(dir, 0755)
	if _, err = New(p); err == nil {
		t.Fatal("insecure control parent accepted")
	}
}
func TestStrictAcknowledgementsTimeoutsAndHeaderPrecedence(t *testing.T) {
	partial, _ := proto.Marshal(&collector.ExportMetricsServiceResponse{PartialSuccess: &collector.ExportMetricsPartialSuccess{RejectedDataPoints: 1}})
	for _, test := range []struct {
		status int
		body   []byte
	}{{200, partial}, {200, []byte{255}}, {200, make([]byte, 65537)}, {503, []byte("not logged")}, {302, nil}} {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(test.status); w.Write(test.body) }))
		rt, err := New(testPlan(server.URL))
		if err != nil {
			t.Fatal(err)
		}
		rt.finish(rt.start("one"), false)
		rt.Close()
		server.Close()
		if rt.exportLoss.Load() == 0 {
			t.Fatal("invalid acknowledgement accepted")
		}
	}
	slow := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-r.Context().Done():
		case <-time.After(200 * time.Millisecond):
		}
	}))
	rt, err := New(testPlan(slow.URL))
	if err != nil {
		t.Fatal(err)
	}
	rt.finish(rt.start("one"), false)
	rt.Close()
	slow.Close()
	if rt.exportLoss.Load() == 0 {
		t.Fatal("timeout accepted")
	}
	for _, test := range []struct {
		generic, signal string
		present         bool
		key, value      string
	}{{"x=hello%20world", "", false, "x", "hello world"}, {"x=old", "x=new+value", true, "x", "new+value"}, {"x=old", "", true, "", ""}} {
		got, err := headers(test.generic, test.signal, test.present)
		if err != nil || got[test.key] != test.value {
			t.Fatal(got, err)
		}
	}
	for _, value := range []string{"invalid", "bad name=value", "x=%0d", "x=%zz"} {
		if _, err = headers(value, "", false); err == nil {
			t.Fatal("invalid header accepted")
		}
	}
	t.Setenv("OTEL_EXPORTER_OTLP_METRICS_HEADERS", "invalid")
	if _, err = New(testPlan("http://127.0.0.1:1")); err == nil {
		t.Fatal("invalid headers accepted at startup")
	}
}
