package probe

import (
	"runtime"
	"testing"
)

func watched(p *Recorder, failed, panics bool) (success bool) {
	completion := p.Watch()
	defer func() { success = completion.End(failed) }()
	if panics {
		panic(42)
	}
	return
}

func TestAuxiliaryCompletionDoesNotInstrumentExcludedFunctions(t *testing.T) {
	h := newHarness(t, true, func(o *Options) { o.Functions = nil })
	if !watched(h.probe, false, false) || watched(h.probe, true, false) {
		t.Fatal("normal/error completion wrong")
	}
	catch(func() { watched(h.probe, false, true) })
	if len(h.exporter.GetSpans()) != 0 || metricCount(t, h, "otelc.function.calls") != 0 {
		t.Fatal("lifetime-only hooks instrumented excluded functions")
	}
	s := h.probe.Snapshot()
	if s.FunctionStarts != 0 || s.ObserverStarts != 3 || s.ObserverEnds != 3 || s.ObserverActive != 0 || s.ObserverLoss != 0 || s.UnwindUnknown != 0 {
		t.Fatalf("bad observer accounting %+v", s)
	}
}

func TestWatchBoundFenceRepeatedEndAndUnknown(t *testing.T) {
	h := newHarness(t, true, func(o *Options) { o.MaxActive = 1 })
	i := h.probe.Start(0)
	if h.probe.Watch() != nil {
		t.Fatal("shared bound exceeded")
	}
	i.End(false)
	c := h.probe.Watch()
	if h.probe.Start(0) != nil || h.probe.Watch() != nil {
		t.Fatal("shared function/observer bound exceeded")
	}
	c.boundary = "missing.boundary"
	first, repeated := c.End(false), c.End(false)
	if first || repeated {
		t.Fatal("unknown or repeated end admitted")
	}
	h.probe.Close()
	if h.probe.Watch() != nil {
		t.Fatal("closed observer admitted")
	}
	var missing *Completion
	if missing.End(false) {
		t.Fatal("nil completion admitted")
	}
	s := h.probe.Snapshot()
	if s.ObserverLoss != 2 || s.FunctionLoss != 1 || s.ObserverEnds != 1 || s.UnwindUnknown != 1 {
		t.Fatalf("accounting %+v", s)
	}
}

func TestWatchGoexitAndPanickingReleaseCannotCompleteResource(t *testing.T) {
	h := newHarness(t, true, nil)
	object := new(testObject)
	Track(h.probe, object, 0)
	catch(func() { c := h.probe.Watch(); defer func() { Release(h.probe, object, c.End(false)) }(); panic(42) })
	done := make(chan struct{})
	go func() {
		defer close(done)
		c := h.probe.Watch()
		defer func() { Release(h.probe, object, c.End(false)) }()
		runtime.Goexit()
	}()
	<-done
	if s := h.probe.Snapshot(); s.Released != 0 || s.OpenResources != 1 || s.ObserverEnds != 2 || s.ObserverActive != 0 {
		t.Fatalf("unwind invented release %+v", s)
	}
	runtime.KeepAlive(object)
}
