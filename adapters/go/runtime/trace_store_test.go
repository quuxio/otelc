package runtime

import (
	"context"
	"encoding/json"
	"strings"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"io.quux.otelc/go/policy"
)

func traceTestPlan(endpoint string) policy.Plan {
	p := testPlan(endpoint)
	if err := json.Unmarshal([]byte(`{"traces":{"enabled":true,"root_sample_ratio":1,"max_active_traces":8,"max_spans_per_trace":32},"trace_export":{"endpoint":"http://127.0.0.1:1/v1/traces","protocol":"http/protobuf","timeout_ms":300},"export":{"max_queued_batches":8}}`), &p); err != nil {
		panic(err)
	}
	return p
}

type captureSpans struct {
	mu       sync.Mutex
	trees    [][]sdktrace.ReadOnlySpan
	deadline time.Time
}

func (c *captureSpans) ExportSpans(ctx context.Context, spans []sdktrace.ReadOnlySpan) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.trees = append(c.trees, spans)
	c.deadline, _ = ctx.Deadline()
	return nil
}
func TestTraceStorePhaseHonoursSharedDeadlineAndDropsExpiredQueue(t *testing.T) {
	store, sender := makeStore(t, traceTestPlan("http://127.0.0.1"))
	root := store.begin(nil, "root", time.Now())
	store.finish(root, time.Now(), false)
	deadline := time.Now().Add(100 * time.Millisecond)
	store.shutdownPending(deadline)
	<-store.flush()
	if !sender.deadline.Equal(deadline) {
		t.Fatal("shared shutdown deadline changed", sender.deadline, deadline)
	}
	root = store.begin(nil, "expired", time.Now())
	store.finish(root, time.Now(), false)
	store.shutdownPending(time.Now().Add(-time.Second))
	<-store.flush()
	if len(sender.trees) != 1 || store.retained != 0 || store.losses["export_deadline"] != 1 {
		t.Fatal("expired tree encoded, retained or loss hidden")
	}
}
func (c *captureSpans) Shutdown(context.Context) error { return nil }
func makeStore(t *testing.T, p policy.Plan) (*traceStore, *captureSpans) {
	t.Helper()
	sender := &captureSpans{}
	rt := &Runtime{plan: p}
	store, err := newTraceStore(rt, resource.NewSchemaless(), sender)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { store.close(context.Background()); store.provider.Shutdown(context.Background()) })
	return store, sender
}
func TestSDKTraceStoreParentsTimesAndEscapingStatus(t *testing.T) {
	store, sender := makeStore(t, traceTestPlan("http://127.0.0.1:1"))
	started := time.Now()
	root := store.begin(nil, "root", started)
	child := store.begin(root, "child", started.Add(time.Millisecond))
	store.finish(child, started.Add(2*time.Millisecond), true)
	store.finish(child, started.Add(3*time.Millisecond), false)
	store.finish(root, started.Add(4*time.Millisecond), false)
	<-store.flush()
	if len(sender.trees) != 1 || len(sender.trees[0]) != 2 {
		t.Fatalf("trees=%v", sender.trees)
	}
	for _, span := range sender.trees[0] {
		if !span.SpanContext().IsValid() || span.EndTime().Before(span.StartTime()) {
			t.Fatal("invalid SDK identity or time")
		}
		if span.Name() == "child" {
			if span.Parent().SpanID() != root.context.SpanID() || span.Status().Code != codes.Error {
				t.Fatal("child parent/status")
			}
		} else if span.Parent().IsValid() {
			t.Fatal("root had a parent")
		}
	}
	if store.retained != 0 || len(store.roots) != 0 || len(store.ready) != 0 {
		t.Fatal("completed records retained")
	}
}
func TestTraceCapacityDiscardsWholeTreeAndReleasesSDKPayload(t *testing.T) {
	p := traceTestPlan("http://127.0.0.1:1")
	p.Traces.MaxSpans = 1
	store, sender := makeStore(t, p)
	root := store.begin(nil, "root", time.Now())
	child := store.begin(root, "child", time.Now())
	if child.sampled || store.retained != 0 || store.roots[root.context.TraceID()].nodes != nil {
		t.Fatal("rejected tree retained SDK payload")
	}
	store.finish(child, time.Now(), false)
	store.finish(root, time.Now(), false)
	<-store.flush()
	if len(sender.trees) != 0 || store.losses["span_capacity"] != 1 {
		t.Fatal("partial tree exported or loss missing")
	}
}
func TestTraceRootQueueSamplingAndIncompleteBounds(t *testing.T) {
	p := traceTestPlan("http://127.0.0.1:1")
	p.Traces.MaxActive = 1
	p.Export.MaxQueued = 1
	store, sender := makeStore(t, p)
	root := store.begin(nil, "one", time.Now())
	rejected := store.begin(nil, "two", time.Now())
	if rejected.sampled || store.losses["trace_capacity"] != 1 {
		t.Fatal("root bound")
	}
	store.finish(root, time.Now(), false)
	next := store.begin(nil, "three", time.Now())
	store.finish(next, time.Now(), false)
	if store.losses["queue_capacity"] != 1 {
		t.Fatal("queue bound")
	}
	store.begin(nil, "incomplete", time.Now())
	store.shutdownPending(time.Now().Add(time.Second))
	<-store.flush()
	if len(sender.trees) != 1 || store.losses["incomplete"] != 1 || store.retained != 0 {
		t.Fatal("shutdown bounds")
	}
	p.Traces.Ratio = 0
	sampled, none := makeStore(t, p)
	identity := sampled.begin(nil, "sampled-out", time.Now())
	sampled.begin(identity, "child", time.Now())
	sampled.finish(identity, time.Now(), true)
	sampled.shutdownPending(time.Now().Add(time.Second))
	<-sampled.flush()
	if len(none.trees) != 0 || sampled.sampledOut != 1 || len(sampled.losses) != 0 {
		t.Fatal("sampling inheritance or incomplete classification")
	}
}
func TestLargeTraceDiscardedBeforeSDKExport(t *testing.T) {
	p := traceTestPlan("http://127.0.0.1:1")
	p.Traces.MaxSpans = 65536
	store, sender := makeStore(t, p)
	name := strings.Repeat("n", 1024)
	root := store.begin(nil, name, time.Now())
	for range 8192 {
		child := store.begin(root, name, time.Now())
		store.finish(child, time.Now(), false)
	}
	store.finish(root, time.Now(), false)
	<-store.flush()
	if len(sender.trees) != 0 || store.losses["batch_bytes"] != 1 || store.retained != 0 {
		t.Fatal("large tree reached the SDK exporter")
	}
}
