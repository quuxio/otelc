package runtime

import (
	"context"
	collector "go.opentelemetry.io/proto/otlp/collector/metrics/v1"
	"google.golang.org/protobuf/proto"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestOTLPMetricsRequires200Acknowledgement(t *testing.T) {
	for _, status := range []int{202, 204} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(status) }))
			defer server.Close()
			rt, err := New(testPlan(server.URL))
			if err != nil {
				t.Fatal(err)
			}
			defer rt.Close()
			rt.finish(rt.start("one"), false)
			if err = rt.provider.ForceFlush(context.Background()); err == nil {
				t.Fatal("non-200 acknowledgement reported as success")
			}
			if rt.exportLoss.Load() == 0 {
				t.Fatal("rejected acknowledgement had no visible loss")
			}
		})
	}
}
func TestRejectedMetricStatusDoesNotWaitForBody(t *testing.T) {
	headers := make(chan struct{})
	release := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(401)
		w.(http.Flusher).Flush()
		close(headers)
		select {
		case <-r.Context().Done():
		case <-release:
		}
	}))
	defer server.Close()
	defer close(release)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, server.URL, nil)
	transport := &http.Transport{}
	defer transport.CloseIdleConnections()
	result := make(chan error, 1)
	go func() { _, err := (strictTransport{transport}).RoundTrip(req); result <- err }()
	<-headers
	select {
	case err := <-result:
		if err == nil {
			t.Fatal("rejected response accepted")
		}
	case <-time.After(time.Second):
		t.Fatal("waited for a stalled rejection body after receiving status")
	}
}
func TestExportHealthOnlyChangeReachesRealSDKPayload(t *testing.T) {
	requests := make(chan *collector.ExportMetricsServiceRequest, 4)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		req := new(collector.ExportMetricsServiceRequest)
		if err := proto.Unmarshal(body, req); err != nil {
			t.Error(err)
		}
		requests <- req
		w.WriteHeader(200)
	}))
	defer server.Close()
	rt, err := New(testPlan(server.URL))
	if err != nil {
		t.Fatal(err)
	}
	defer rt.Close()
	rt.finish(rt.start("one"), false)
	if err = rt.provider.ForceFlush(context.Background()); err != nil {
		t.Fatal(err)
	}
	<-requests
	// Operational export failure changes health without another application invocation.
	rt.exportLoss.Add(1)
	if err = rt.provider.ForceFlush(context.Background()); err != nil {
		t.Fatal(err)
	}
	select {
	case request := <-requests:
		var health, calls int64
		for _, resource := range request.ResourceMetrics {
			for _, scope := range resource.ScopeMetrics {
				for _, metric := range scope.Metrics {
					if metric.GetSum() == nil {
						continue
					}
					for _, point := range metric.GetSum().DataPoints {
						switch metric.Name {
						case "otelc.export.dropped_batches":
							health += point.GetAsInt()
						case "otelc.function.calls":
							calls += point.GetAsInt()
						}
					}
				}
			}
		}
		if health != 1 || calls != 1 {
			t.Fatalf("health=%d calls=%d", health, calls)
		}
	default:
		t.Fatal("health-only change was suppressed; no SDK payload reached the receiver")
	}
}
