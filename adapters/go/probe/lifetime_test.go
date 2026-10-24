package probe

import (
	"context"
	"runtime"
	"testing"
	"time"

	"go.opentelemetry.io/otel/metric/noop"
	"go.opentelemetry.io/otel/trace"
	tracenoop "go.opentelemetry.io/otel/trace/noop"
)

type testObject struct{ bytes [32]byte }

func awaitGC(t *testing.T, p *Recorder, count uint64) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		runtime.GC()
		if p.Snapshot().GCObserved == count {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("GC observation did not arrive: %+v", p.Snapshot())
}

func makeTracked(t *testing.T, p *Recorder, id int) {
	t.Helper()
	object := new(testObject)
	if !Track(p, object, id) {
		t.Fatal("object not admitted")
	}
	runtime.KeepAlive(object)
}

func TestWeakLifetimesDistinguishFailedDuplicateReleaseAndObservedGC(t *testing.T) {
	h := newHarness(t, true, nil)
	func() {
		object := new(testObject)
		if !Track(h.probe, object, 0) || !Track(h.probe, object, 0) {
			t.Fatal("track failed")
		}
		Release(h.probe, object, false)
		if h.probe.Snapshot().Released != 0 {
			t.Fatal("failed release completed")
		}
		Release(h.probe, object, true)
		Release(h.probe, object, true)
		state := h.probe.Snapshot()
		if state.ObjectsObserved != 1 || state.Released != 1 || state.GCObserved != 0 || state.DuplicateObjects != 1 || state.OpenResources != 0 {
			t.Fatalf("bad release state: %+v", state)
		}
		runtime.KeepAlive(object)
	}()
	awaitGC(t, h.probe, 1)
	if s := h.probe.Snapshot(); s.LiveObjects != 0 || s.LifetimeLoss != 0 || s.OpenResources != 0 {
		t.Fatalf("health %+v", s)
	}
	spans := h.exporter.GetSpans()
	if len(spans) != 2 {
		t.Fatalf("spans=%d", len(spans))
	}
	if metricCount(t, h, "otelc.lifetime.admitted") != 2 || metricCount(t, h, "otelc.lifetime.completed") != 2 || metricCount(t, h, "otelc.lifetime.incomplete") != 0 {
		t.Fatal("missing lifetime metrics")
	}
}

func TestGCDoesNotInventResourceRelease(t *testing.T) {
	h := newHarness(t, true, nil)
	makeTracked(t, h.probe, 0)
	awaitGC(t, h.probe, 1)
	state := h.probe.Snapshot()
	if state.Released != 0 || state.OpenResources != 1 || state.LiveObjects != 0 {
		t.Fatalf("bad unreleased state %+v", state)
	}
	if len(h.exporter.GetSpans()) != 1 {
		t.Fatal("GC completed resource span")
	}
}

func TestCloseKeepsOutstandingBoundariesUnresolved(t *testing.T) {
	h := newHarness(t, true, nil)
	object := new(testObject)
	Track(h.probe, object, 0)
	h.probe.Close()
	h.probe.Close()
	Release(h.probe, object, true)
	if Track(h.probe, object, 0) {
		t.Fatal("closed observer tracked")
	}
	s := h.probe.Snapshot()
	if s.GCObserved != 0 || s.Released != 0 || s.LiveObjects != 1 || s.OpenResources != 1 || !s.Closed {
		t.Fatalf("shutdown invented completion %+v", s)
	}
	if len(h.exporter.GetSpans()) != 0 {
		t.Fatal("shutdown ended lifetime span")
	}
	runtime.KeepAlive(object)
}

func TestLifetimeBoundsNilSmallAndPlainObjects(t *testing.T) {
	h := newHarness(t, true, func(o *Options) { o.MaxLive = 1 })
	var nilObject *testObject
	if Track(h.probe, nilObject, 0) {
		t.Fatal("nil tracked")
	}
	Release(h.probe, nilObject, true)
	if Track(h.probe, new(byte), 0) || Track(h.probe, new(testObject), -1) {
		t.Fatal("unsupported object tracked")
	}
	object := new(testObject)
	if !Track(h.probe, object, 1) {
		t.Fatal("plain object refused")
	}
	Release(h.probe, object, true)
	Release(h.probe, new(testObject), true)
	if Track(h.probe, new(testObject), 0) {
		t.Fatal("capacity exceeded")
	}
	if s := h.probe.Snapshot(); s.LifetimeLoss != 3 || s.OpenResources != 0 {
		t.Fatalf("bad capacity %+v", s)
	}
	runtime.KeepAlive(object)
}

func TestLifetimeCollectionGatesDoNotSuppressSpanBoundaries(t *testing.T) {
	h := newHarness(t, false, nil)
	func() {
		object := new(testObject)
		Track(h.probe, object, 0)
		h.controls.SetCollection(true)
		Release(h.probe, object, true)
		runtime.KeepAlive(object)
	}()
	awaitGC(t, h.probe, 1)
	if len(h.exporter.GetSpans()) != 2 || metricCount(t, h, "otelc.lifetime.admitted") != 0 || metricCount(t, h, "otelc.lifetime.completed") != 0 {
		t.Fatal("startup-off generated retroactive lifetime metrics or suppressed traces")
	}
}

func TestDetachFailuresRemainLoss(t *testing.T) {
	for _, failAt := range []int{1, 2} {
		calls := 0
		h := newHarness(t, true, func(o *Options) { o.Detach = func(trace.Span) bool { calls++; return calls != failAt } })
		if Track(h.probe, new(testObject), 0) {
			t.Fatal("detach failure admitted")
		}
		if s := h.probe.Snapshot(); s.LifetimeLoss != 1 || s.ObjectsObserved != 0 {
			t.Fatalf("missing detach failure %+v", s)
		}
	}
}

func TestCleanupAfterFenceDoesNotChangeHealth(t *testing.T) {
	h := newHarness(t, true, nil)
	object := new(testObject)
	Track(h.probe, object, 0)
	var entry *lifetime
	for _, e := range h.probe.objects {
		entry = e
	}
	h.probe.Close()
	before := h.probe.Snapshot()
	entry.observedGC()
	if h.probe.Snapshot() != before {
		t.Fatal("cleanup after close changed health")
	}
	runtime.KeepAlive(object)
}

func TestPanickingReleaseWithNilErrorReturnDoesNotCompleteResource(t *testing.T) {
	h := newHarness(t, true, nil)
	object := new(testObject)
	Track(h.probe, object, 0)
	catch(func() {
		invocation := h.probe.Start(0)
		defer func() { Release(h.probe, object, invocation.End(false)) }()
		panic(42)
	})
	if s := h.probe.Snapshot(); s.Released != 0 || s.OpenResources != 1 {
		t.Fatalf("panic invented a successful release: %+v", s)
	}
	runtime.KeepAlive(object)
}

func TestWeakObserverWorksWithTracingDisabled(t *testing.T) {
	p, err := New(noop.NewMeterProvider().Meter("fixture"), tracenoop.NewTracerProvider().Tracer("fixture"),
		Options{Lifetimes: []Lifetime{{"Object", true}}, MaxActive: 1, MaxLive: 1,
			Collecting: func() bool { return true }, Parent: func(ctx context.Context) context.Context { return ctx },
			Detach: func(trace.Span) bool { t.Fatal("no-op span required detachment"); return false }})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(p.Close)
	func() {
		object := new(testObject)
		if !Track(p, object, 0) {
			t.Fatal("no-trace lifetime refused")
		}
		Release(p, object, true)
		runtime.KeepAlive(object)
	}()
	awaitGC(t, p, 1)
	if state := p.Snapshot(); state.Released != 1 || state.LifetimeLoss != 0 {
		t.Fatalf("disabled tracing changed lifetime observation: %+v", state)
	}
}
