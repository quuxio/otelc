package runtime

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"go.opentelemetry.io/otel/trace"
)

func TestEnvironmentBootstrapSharesOneOwnedSDKAndPinsBinding(t *testing.T) {
	sink := &receiver{}
	server := httptest.NewServer(http.HandlerFunc(sink.serve))
	t.Cleanup(server.Close)
	directory := privateTemp(t)
	cfg := configuration(server.URL)
	data, err := json.Marshal(cfg)
	if err != nil {
		t.Fatal(err)
	}
	digest := configFile(t, directory, data)
	t.Setenv("OTELC_GO_RUNTIME", directory)
	t.Setenv("OTELC_GO_CONFIG_SHA256", digest)
	parent := func(ctx context.Context) context.Context { return ctx }
	detach := func(trace.Span) bool { return true }
	first, err := FromEnvironment(parent, detach)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = first.Close() })
	second, err := FromEnvironment(parent, detach)
	if err != nil || first != second {
		t.Fatal("created multiple SDKs")
	}
	t.Setenv("OTELC_GO_CONFIG_SHA256", strings.Repeat("0", 64))
	if _, err := FromEnvironment(parent, detach); err == nil {
		t.Fatal("changed bootstrap accepted")
	}
	var invalid loader
	if _, err := invalid.get(directory, "bad", parent, detach); err == nil {
		t.Fatal("invalid first config accepted")
	}
}

func TestPeriodicRuntimeKeepsTracingIndependentFromBothMetricGates(t *testing.T) {
	sink := &receiver{}
	server := httptest.NewServer(http.HandlerFunc(sink.serve))
	t.Cleanup(server.Close)
	cfg := configuration(server.URL)
	cfg.IntervalMS = 5
	cfg.Collection, cfg.Emission = false, false
	r := startRuntime(t, cfg)
	invoke(r)
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		_, spans, _ := sink.snapshot()
		if spans == 1 {
			break
		}
		time.Sleep(time.Millisecond)
	}
	metrics, spans, _ := sink.snapshot()
	if metrics != 0 || spans != 1 {
		t.Fatal("periodic tracing was gated by metrics")
	}
	command(t, r, "on")
	// A periodic export can contain the live gauges before the selected call.
	// Exercise that order explicitly; request arrival alone is not call receipt.
	deadline = time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		metrics, _, _ := sink.snapshot()
		if metrics > 0 {
			break
		}
		time.Sleep(time.Millisecond)
	}
	if metrics, _, calls := sink.snapshot(); metrics == 0 || calls != 0 {
		t.Fatal("missing gauge-only periodic export before the selected call")
	}
	invoke(r)
	deadline = time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		metrics, _, calls := sink.snapshot()
		if metrics > 0 && calls == 1 {
			break
		}
		time.Sleep(time.Millisecond)
	}
	if metrics, _, calls := sink.snapshot(); metrics == 0 || calls != 1 {
		t.Fatal("startup-off could not enable periodic metrics")
	}
	command(t, r, "collection off")
	// Collection-only control does not wait for an already admitted export.
	// Flush serialises with that export before we count later periodic work.
	if response := command(t, r, "flush"); response["error"] != nil {
		t.Fatal("could not settle the admitted periodic export")
	}
	before, _, _ := sink.snapshot()
	time.Sleep(30 * time.Millisecond)
	if after, _, _ := sink.snapshot(); after != before {
		t.Fatal("collection-off periodic snapshot leaked")
	}
}
