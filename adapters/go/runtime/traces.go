package runtime

import (
	"context"
	"fmt"
	"math"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
	"io.quux.otelc/go/policy"
)

const maxTraceRecords = 1048576
const maxTraceBytes = 16 * 1024 * 1024

type traceSender interface {
	ExportSpans(context.Context, []sdktrace.ReadOnlySpan) error
	Shutdown(context.Context) error
}
type traceIdentity struct {
	context  trace.SpanContext
	started  time.Time
	sampled  bool
	finished bool
}
type traceTree struct {
	root            trace.SpanID
	origin          time.Time
	nodes           map[trace.SpanID]trace.Span
	active          int
	closed, invalid bool
	bytes           int
}
type traceStore struct {
	mu                              sync.Mutex
	rt                              *Runtime
	provider                        *sdktrace.TracerProvider
	tracer                          trace.Tracer
	sender                          traceSender
	roots                           map[trace.TraceID]*traceTree
	ready                           []*traceTree
	retained, completed, sampledOut int
	losses                          map[string]uint64
	activeFlush                     chan struct{}
	flushCancel                     context.CancelFunc
	shutdownDeadline                time.Time
}

func validateTracePlan(p policy.Plan) error {
	if !p.Traces.Enabled {
		return nil
	}
	valid := func(n, max int) bool { return n > 0 && n <= max }
	if p.TraceExport == nil || math.IsNaN(p.Traces.Ratio) || math.IsInf(p.Traces.Ratio, 0) || p.Traces.Ratio < 0 || p.Traces.Ratio > 1 || !valid(p.Traces.MaxActive, 65536) || !valid(p.Traces.MaxSpans, 65536) || p.Traces.MaxActive*p.Traces.MaxSpans > maxTraceRecords || !valid(p.Export.MaxQueued, 64) || !valid(p.TraceExport.TimeoutMS, 60000) || p.TraceExport.Protocol != "http/protobuf" || !valid(p.Runtime.MaxFunctions, maxTraceRecords) || !valid(p.Runtime.MaxActive, 65536) || !valid(p.Runtime.ShutdownMS, 60000) {
		return fmt.Errorf("invalid resolved Go trace settings")
	}
	u, err := url.Parse(p.TraceExport.Endpoint)
	if err != nil {
		return fmt.Errorf("invalid resolved Go trace endpoint")
	}
	host := strings.ToLower(u.Hostname())
	port := u.Port()
	portNumber, portErr := strconv.Atoi(port)
	if host == "" || u.User != nil || strings.ContainsAny(p.TraceExport.Endpoint, "?#") || (port != "" && (portErr != nil || portNumber < 1 || portNumber > 65535)) || !(u.Scheme == "https" || u.Scheme == "http" && (host == "localhost" || host == "127.0.0.1" || host == "::1")) {
		return fmt.Errorf("invalid resolved Go trace endpoint")
	}
	return nil
}
func newTraceStore(rt *Runtime, res *resource.Resource, sender traceSender) (*traceStore, error) {
	if err := validateTracePlan(rt.plan); err != nil {
		return nil, err
	}
	provider := sdktrace.NewTracerProvider(sdktrace.WithResource(res), sdktrace.WithSampler(sdktrace.ParentBased(sdktrace.TraceIDRatioBased(rt.plan.Traces.Ratio))), sdktrace.WithRawSpanLimits(sdktrace.SpanLimits{AttributeCountLimit: 1, AttributeValueLengthLimit: 1024, EventCountLimit: 0, LinkCountLimit: 0}))
	return &traceStore{rt: rt, provider: provider, tracer: provider.Tracer("quux.otelc", trace.WithInstrumentationVersion("0.1.0")), sender: sender, roots: map[trace.TraceID]*traceTree{}, losses: map[string]uint64{}}, nil
}
func (s *traceStore) reject(parent *traceIdentity, reason string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.rejectLocked(parent, reason)
}

// Unknown goroutine identity may belong to any active tree. Keep no partial tree.
func (s *traceStore) rejectAll(reason string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.roots) == 0 {
		s.losses[reason]++
	}
	for _, tree := range s.roots {
		if !tree.invalid {
			tree.invalid = true
			s.retained -= len(tree.nodes)
			tree.nodes = nil
			s.losses[reason]++
		}
	}
}
func (s *traceStore) rejectLocked(parent *traceIdentity, reason string) {
	if parent == nil {
		s.losses[reason]++
		return
	}
	if !parent.sampled {
		return
	}
	tree := s.roots[parent.context.TraceID()]
	if tree != nil && !tree.invalid {
		tree.invalid = true
		s.retained -= len(tree.nodes)
		tree.nodes = nil
		s.losses[reason]++
	}
}
func (s *traceStore) begin(parent *traceIdentity, name string, started time.Time) *traceIdentity {
	s.mu.Lock()
	defer s.mu.Unlock()
	ctx := context.Background()
	var tree *traceTree
	epoch := started
	if parent != nil {
		if !parent.sampled {
			return &traceIdentity{}
		}
		tree = s.roots[parent.context.TraceID()]
		if tree == nil || tree.invalid {
			return &traceIdentity{}
		}
		if len(tree.nodes) >= s.rt.plan.Traces.MaxSpans || s.retained >= maxTraceRecords {
			s.rejectLocked(parent, "span_capacity")
			return &traceIdentity{}
		}
		ctx = trace.ContextWithSpanContext(ctx, parent.context)
		epoch = tree.origin.Add(max(started.Sub(tree.origin), 0))
	}
	_, span := s.tracer.Start(ctx, name, trace.WithTimestamp(epoch), trace.WithAttributes(attribute.String("code.function.name", name)))
	identity := span.SpanContext()
	if !identity.IsValid() {
		s.rejectLocked(parent, "invalid")
		return &traceIdentity{}
	}
	if !identity.IsSampled() {
		s.sampledOut++
		return &traceIdentity{}
	}
	if parent == nil {
		if len(s.roots) >= s.rt.plan.Traces.MaxActive || s.retained >= maxTraceRecords {
			s.rejectLocked(nil, "trace_capacity")
			return &traceIdentity{}
		}
		if s.roots[identity.TraceID()] != nil {
			s.rejectLocked(nil, "invalid")
			return &traceIdentity{}
		}
		tree = &traceTree{root: identity.SpanID(), origin: started, nodes: map[trace.SpanID]trace.Span{}}
		s.roots[identity.TraceID()] = tree
	}
	if tree.nodes[identity.SpanID()] != nil {
		s.rejectLocked(parent, "invalid")
		return &traceIdentity{}
	}
	tree.nodes[identity.SpanID()] = span
	tree.active++
	tree.bytes += 2*len(name) + 256
	s.retained++
	return &traceIdentity{context: identity, started: started, sampled: true}
}
func (s *traceStore) finish(identity *traceIdentity, ended time.Time, escaped bool) {
	if identity == nil || !identity.sampled {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if identity.finished {
		return
	}
	identity.finished = true
	tree := s.roots[identity.context.TraceID()]
	if tree == nil {
		return
	}
	if !tree.invalid {
		span := tree.nodes[identity.context.SpanID()]
		if span == nil {
			s.rejectLocked(identity, "invalid")
			return
		}
		if escaped {
			span.SetStatus(codes.Error, "escaping unwind")
		}
		elapsed := max(ended.Sub(identity.started), 0)
		span.End(trace.WithTimestamp(tree.origin.Add(max(identity.started.Sub(tree.origin), 0) + elapsed)))
	}
	tree.active--
	if identity.context.SpanID() == tree.root {
		tree.closed = true
	}
	if tree.active != 0 || !tree.closed {
		return
	}
	delete(s.roots, identity.context.TraceID())
	if tree.invalid {
		return
	}
	if len(s.ready) >= s.rt.plan.Export.MaxQueued {
		s.retained -= len(tree.nodes)
		s.losses["queue_capacity"]++
		return
	}
	s.completed++
	s.ready = append(s.ready, tree)
}
func (s *traceStore) report() map[string]any {
	s.mu.Lock()
	defer s.mu.Unlock()
	lost := map[string]uint64{}
	for reason, count := range s.losses {
		lost[reason] = count
	}
	return map[string]any{"completed_trees": s.completed, "sampled_out_roots": s.sampledOut, "active_trees": len(s.roots), "queued_trees": len(s.ready), "losses": lost}
}
func (s *traceStore) shutdownPending(deadline time.Time) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.shutdownDeadline = deadline
	for _, tree := range s.roots {
		s.retained -= len(tree.nodes)
		if !tree.invalid {
			s.losses["incomplete"]++
		}
	}
	clear(s.roots)
}
func (s *traceStore) flush() <-chan struct{} {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.activeFlush != nil {
		return s.activeFlush
	}
	done := make(chan struct{})
	s.activeFlush = done
	deadline := time.Now().Add(time.Duration(s.rt.plan.TraceExport.TimeoutMS) * time.Millisecond)
	if !s.shutdownDeadline.IsZero() && s.shutdownDeadline.Before(deadline) {
		deadline = s.shutdownDeadline
	}
	ctx, cancel := context.WithDeadline(context.Background(), deadline)
	s.flushCancel = cancel
	go func() {
		defer func() { s.mu.Lock(); s.activeFlush = nil; s.flushCancel = nil; close(done); s.mu.Unlock() }()
		defer cancel()
		for range s.rt.plan.Export.MaxQueued {
			s.mu.Lock()
			if len(s.ready) == 0 {
				s.mu.Unlock()
				return
			}
			tree := s.ready[0]
			s.ready[0] = nil
			s.ready = s.ready[1:]
			s.retained -= len(tree.nodes)
			if ctx.Err() != nil {
				s.losses["export_deadline"]++
				s.rt.exportLoss.Add(1)
				s.mu.Unlock()
				continue
			}
			if tree.bytes > maxTraceBytes {
				s.losses["batch_bytes"]++
				s.mu.Unlock()
				continue
			}
			spans := make([]sdktrace.ReadOnlySpan, 0, len(tree.nodes))
			for _, span := range tree.nodes {
				spans = append(spans, span.(sdktrace.ReadOnlySpan))
			}
			s.mu.Unlock()
			if err := s.sender.ExportSpans(ctx, spans); err != nil {
				s.rt.exportLoss.Add(1)
			}
		}
	}()
	return done
}
func (s *traceStore) close(ctx context.Context) error {
	s.mu.Lock()
	if s.flushCancel != nil {
		s.flushCancel()
	}
	for _, tree := range s.ready {
		s.retained -= len(tree.nodes)
		s.losses["shutdown"]++
		s.rt.exportLoss.Add(1)
	}
	s.ready = nil
	s.mu.Unlock()
	transportErr := s.sender.Shutdown(ctx)
	providerErr := s.provider.Shutdown(ctx)
	if transportErr != nil {
		return transportErr
	}
	return providerErr
}
