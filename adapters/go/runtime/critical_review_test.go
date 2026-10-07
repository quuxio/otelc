package runtime

import (
	"bytes"
	"context"
	sdk "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	collector "go.opentelemetry.io/proto/otlp/collector/metrics/v1"
	"google.golang.org/protobuf/proto"
	"io"
	"net/http"
	"net/http/httptest"
	goruntime "runtime"
	"strings"
	"testing"
	"time"
)

func TestCriticalReviewExportCollectedBeforeRevisionChanges(t *testing.T) {
	counts := []int64{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		payload, _ := io.ReadAll(r.Body)
		req := new(collector.ExportMetricsServiceRequest)
		if err := proto.Unmarshal(payload, req); err != nil {
			t.Error(err)
		}
		total := int64(0)
		for _, rm := range req.ResourceMetrics {
			for _, sm := range rm.ScopeMetrics {
				for _, m := range sm.Metrics {
					if m.Name == "otelc.function.calls" {
						for _, dp := range m.GetSum().DataPoints {
							total += dp.GetAsInt()
						}
					}
				}
			}
		}
		counts = append(counts, total)
		w.WriteHeader(200)
	}))
	defer server.Close()
	rt := &Runtime{plan: testPlan(server.URL)}
	reader := sdk.NewManualReader()
	provider := sdk.NewMeterProvider(sdk.WithReader(reader))
	defer provider.Shutdown(context.Background())
	meter := provider.Meter("critical-review")
	counter, _ := meter.Int64Counter("otelc.function.calls")
	counter.Add(context.Background(), 1)
	rt.revision.Store(1)
	first := new(metricdata.ResourceMetrics)
	if err := reader.Collect(context.Background(), first); err != nil {
		t.Fatal(err)
	}
	// A call finishes after collection, before PeriodicReader invokes Export.
	counter.Add(context.Background(), 1)
	rt.revision.Store(2)
	exp, err := newExporter(rt)
	if err != nil {
		t.Fatal(err)
	}
	defer exp.Shutdown(context.Background())
	if err = exp.Export(context.Background(), first); err != nil {
		t.Fatal(err)
	}
	second := new(metricdata.ResourceMetrics)
	if err = reader.Collect(context.Background(), second); err != nil {
		t.Fatal(err)
	}
	if err = exp.Export(context.Background(), second); err != nil {
		t.Fatal(err)
	}
	t.Logf("collector received cumulative counts %v; second SDK snapshot contains 2", counts)
	if len(counts) != 2 || counts[1] != 2 {
		t.Fatalf("newer cumulative snapshot suppressed: %v", counts)
	}
}
func TestCriticalReviewDisableWhileEnterWaits(t *testing.T) {
	rt := &Runtime{plan: testPlan("http://127.0.0.1:1"), functions: map[string]*function{}, pending: map[uint64]frame{}, losses: map[string]uint64{}}
	rt.enabled.Store(true)
	rt.mu.Lock()
	admitted := make(chan uint64, 1)
	go func() { admitted <- rt.start("waiting") }()
	deadline := time.Now().Add(time.Second)
	blocked := false
	for time.Now().Before(deadline) {
		stack := make([]byte, 65536)
		n := goruntime.Stack(stack, true)
		for _, s := range bytes.Split(stack[:n], []byte("\n\n")) {
			if bytes.Contains(s, []byte("(*Runtime).start")) && strings.Contains(string(s), "sync.Mutex.Lock") {
				blocked = true
			}
		}
		if blocked {
			break
		}
		goruntime.Gosched()
	}
	if !blocked {
		rt.mu.Unlock()
		t.Fatal("failed to synchronise waiting admission")
	}
	rt.enabled.Store(false)
	rt.mu.Unlock()
	token := <-admitted
	t.Logf("metrics_enabled=%v admitted token=%d", rt.enabled.Load(), token)
	if token != 0 {
		t.Fatal("call admitted after disable while waiting for runtime mutex")
	}
}
