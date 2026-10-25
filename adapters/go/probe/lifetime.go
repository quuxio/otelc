package probe

import (
	"context"
	"runtime"
	"time"
	"unsafe"
	"weak"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/trace"
)

type lifetime struct {
	owner    *Recorder
	key      any // A comparable weak.Pointer[T], never a strong application pointer.
	name     string
	started  time.Time
	collect  bool
	parent   context.Context
	gc       trace.Span
	resource trace.Span
	cleanup  runtime.Cleanup
}

func (p *Recorder) lifetimeSpan(name, boundary string, started time.Time) (trace.Span, bool) {
	parent := trace.SpanContextFromContext(p.parent())
	options := []trace.SpanStartOption{trace.WithNewRoot(), trace.WithTimestamp(started),
		trace.WithAttributes(attribute.String("type", name), attribute.String("boundary", boundary))}
	if parent.IsValid() {
		options = append(options, trace.WithLinks(trace.Link{SpanContext: parent}))
	}
	_, span := p.tracer.Start(context.Background(), name+" lifetime", options...)
	if span.SpanContext().IsValid() && !p.options.Detach(span) {
		failLifetimeSpan(span)
		return nil, false
	}
	return span, true
}

func failLifetimeSpan(span trace.Span) {
	span.SetAttributes(attribute.String("boundary", "observer_failure"))
	span.End()
}

func labels(name, boundary string) metric.MeasurementOption {
	return metric.WithAttributes(attribute.String("type", name), attribute.String("boundary", boundary))
}

func (p *Recorder) rejectLifetime(name, reason string) {
	p.lifeLost++
	p.lifetimeLost.Add(context.Background(), 1, labels(name, reason))
}

// Track is injected after a successful selected allocation/factory return. A
// typed weak identity deduplicates repeated aliases without application methods.
// Pointer-free objects smaller than 16 bytes may share tiny allocation blocks;
// refuse that size profile rather than promise an independent GC observation.
func Track[T any](p *Recorder, object *T, id int) bool {
	if object == nil {
		return false
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	defer runtime.KeepAlive(object)
	if p.closed {
		return false
	}
	if id < 0 || id >= len(p.options.Lifetimes) || unsafe.Sizeof(*object) < 16 {
		p.rejectLifetime("unknown", "unsupported_binding_or_size")
		return false
	}
	key := weak.Make(object)
	if _, exists := p.objects[key]; exists {
		p.duplicates++
		return true
	}
	if len(p.objects) == p.options.MaxLive {
		p.rejectLifetime(p.options.Lifetimes[id].Name, "capacity")
		return false
	}
	binding := p.options.Lifetimes[id]
	started := time.Now()
	gc, ok := p.lifetimeSpan(binding.Name, "observed_gc", started)
	if !ok {
		p.rejectLifetime(binding.Name, "context_detach")
		return false
	}
	var resource trace.Span
	if binding.Resource {
		resource, ok = p.lifetimeSpan(binding.Name, "resource", started)
		if !ok {
			// The GC span is not published as a completed object lifetime.
			failLifetimeSpan(gc)
			p.rejectLifetime(binding.Name, "context_detach")
			return false
		}
	}
	entry := &lifetime{owner: p, key: key, name: binding.Name, started: started,
		collect: p.options.Collecting(), gc: gc, resource: resource,
		parent: trace.ContextWithSpanContext(context.Background(), trace.SpanContextFromContext(p.parent()))}
	p.objects[key] = entry
	entry.cleanup = runtime.AddCleanup(object, (*lifetime).observedGC, entry)
	p.observed++
	if entry.collect {
		p.admitted.Add(entry.parent, 1, labels(entry.name, "observed_gc"))
		if binding.Resource {
			p.admitted.Add(entry.parent, 1, labels(entry.name, "resource"))
		}
	}
	return true
}

func (entry *lifetime) observedGC() {
	p := entry.owner
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.closed || p.objects[entry.key] != entry {
		return
	}
	delete(p.objects, entry.key)
	p.collected++
	if entry.resource != nil {
		p.unreleased++
		if entry.collect {
			p.incomplete.Add(entry.parent, 1, labels(entry.name, "resource"))
		}
	}
	entry.gc.End()
	if entry.collect {
		p.completed.Add(entry.parent, 1, labels(entry.name, "observed_gc"))
		p.lifetimeDuration.Record(entry.parent, time.Since(entry.started).Seconds(), labels(entry.name, "observed_gc"))
	}
}

// Release is injected only after the original configured release returned
// successfully. Failed/panicking releases and duplicate calls cannot end a span.
func Release[T any](p *Recorder, object *T, succeeded bool) {
	if object == nil || !succeeded {
		return
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	defer runtime.KeepAlive(object)
	entry := p.objects[weak.Make(object)]
	if p.closed || entry == nil || entry.resource == nil {
		return
	}
	entry.resource.End()
	entry.resource = nil
	p.releases++
	if entry.collect {
		p.completed.Add(entry.parent, 1, labels(entry.name, "resource"))
		p.lifetimeDuration.Record(entry.parent, time.Since(entry.started).Seconds(), labels(entry.name, "resource"))
	}
}
