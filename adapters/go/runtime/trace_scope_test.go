package runtime

import (
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"

	traceproto "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	pb "go.opentelemetry.io/proto/otlp/trace/v1"
	"google.golang.org/protobuf/proto"
	"io.quux.otelc/go/policy"
)

func TestTraceLegacyPanicModePreservesPropagationAndDiscardsTree(t *testing.T) {
	rt, requests := tracedRuntime(t, nil)
	parent := StartTrace("parent")
	t.Setenv("GODEBUG", "panicnil=1")
	marker := &struct{}{}
	func() {
		defer func() {
			if recover() != marker {
				t.Error("legacy panic identity changed")
			}
		}()
		func() { defer FinishTrace(StartTrace("legacy")); panic(marker) }()
	}()
	t.Setenv("GODEBUG", "panicnil=0")
	FinishTrace(parent)
	func() { defer FinishTrace(StartTrace("later")) }()
	rt.Close()
	spans := storedSpans(*requests)
	if len(spans) != 1 || spans[0].Name != "later" || rt.losses["unsupported_runtime"] != 1 {
		t.Fatal("legacy panic exported partial tree or hid loss")
	}
}
func TestTraceActiveLimitSuppressesNewRootsUntilRejectedActivationFinishes(t *testing.T) {
	rt, requests := tracedRuntime(t, func(p *policy.Plan) { p.Runtime.MaxActive = 1 })
	parent := StartTrace("parent")
	rejected := StartTrace("child")
	FinishTrace(rejected)
	started, release, finished := make(chan struct{}), make(chan struct{}), make(chan struct{})
	go func() {
		scope := StartTrace("rejected-other-goroutine")
		close(started)
		<-release
		FinishTrace(scope)
		close(finished)
	}()
	<-started
	FinishTrace(parent)
	var wait sync.WaitGroup
	wait.Go(func() { defer FinishTrace(StartTrace("suppressed-other-goroutine")) })
	wait.Wait()
	close(release)
	<-finished
	FinishTrace(StartTrace("later"))
	rt.Close()
	spans := storedSpans(*requests)
	if len(spans) != 1 || spans[0].Name != "later" || rt.untrackedScopes != 0 || len(rt.traceCurrent) != 0 {
		t.Fatal("capacity admitted partial trees or failed to recover", rt.report())
	}
}
func TestTraceScopeNoRuntimeFallbackAndClosedAdmission(t *testing.T) {
	active.Store(nil)
	FinishTrace(StartTrace("none"))
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) }))
	defer server.Close()
	rt, err := New(testPlan(server.URL))
	if err != nil {
		t.Fatal(err)
	}
	active.Store(rt)
	defer active.Store(nil)
	FinishTrace(StartTrace("fallback"))
	rt.Close()
	FinishTrace(StartTrace("closed"))
	if rt.report()["function_calls"] != uint64(1) {
		t.Fatal("metrics-only compatibility changed")
	}
	// A closed trace runtime also ignores stale generated defers.
	traced, _ := tracedRuntime(t, nil)
	scope := StartTrace("pending")
	traced.Close()
	FinishTrace(scope)
	traced.discardScope(scope)
	if StartTrace("closed").rt != nil {
		t.Fatal("closed trace runtime admitted calls")
	}
}

func tracedRuntime(t *testing.T, configure func(*policy.Plan)) (*Runtime, *[]*traceproto.ExportTraceServiceRequest) {
	t.Helper()
	requests := []*traceproto.ExportTraceServiceRequest{}
	var mu sync.Mutex
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if r.URL.Path == "/v1/traces" {
			request := new(traceproto.ExportTraceServiceRequest)
			if err := proto.Unmarshal(body, request); err != nil {
				t.Error(err)
			}
			mu.Lock()
			requests = append(requests, request)
			mu.Unlock()
		}
		w.WriteHeader(200)
	}))
	p := traceTestPlan(server.URL + "/v1/metrics")
	p.TraceExport.Endpoint = server.URL + "/v1/traces"
	if configure != nil {
		configure(&p)
	}
	rt, err := New(p)
	if err != nil {
		server.Close()
		t.Fatal(err)
	}
	active.Store(rt)
	t.Cleanup(func() { rt.Close(); active.Store(nil); server.Close() })
	return rt, &requests
}
func storedSpans(requests []*traceproto.ExportTraceServiceRequest) []*pb.Span {
	spans := []*pb.Span{}
	for _, request := range requests {
		for _, resource := range request.ResourceSpans {
			for _, scope := range resource.ScopeSpans {
				spans = append(spans, scope.Spans...)
			}
		}
	}
	return spans
}
func TestTraceScopesPreservePanicRecoveryRecursionAndDeferredCleanup(t *testing.T) {
	rt, requests := tracedRuntime(t, nil)
	var recursive func(int) int
	recursive = func(n int) int {
		defer FinishTrace(StartTrace("recursive"))
		if n == 0 {
			return 0
		}
		return recursive(n-1) + 1
	}
	if recursive(3) != 3 {
		t.Fatal("recursion result")
	}
	func() {
		defer FinishTrace(StartTrace("parent"))
		defer func() { defer FinishTrace(StartTrace("cleanup")) }()
		func() { defer FinishTrace(StartTrace("child")) }()
	}()
	marker := &struct{ value string }{"original private panic"}
	func() {
		defer func() {
			if recover() != marker {
				t.Error("original panic identity changed")
			}
		}()
		func() { defer FinishTrace(StartTrace("escaping")); panic(marker) }()
	}()
	caught := func() (value int) {
		defer FinishTrace(StartTrace("caught"))
		defer func() {
			if recover() == marker {
				value = 7
			}
		}()
		panic(marker)
	}
	if caught() != 7 {
		t.Fatal("direct recover changed")
	}
	rt.Close()
	spans := storedSpans(*requests)
	if len(spans) != 9 || len(*requests) != 4 {
		t.Fatalf("spans=%d trees=%d", len(spans), len(*requests))
	}
	errors, roots := 0, 0
	for _, span := range spans {
		if len(span.ParentSpanId) == 0 {
			roots++
		}
		if span.GetStatus().GetCode() == pb.Status_STATUS_CODE_ERROR {
			errors++
		}
		if span.EndTimeUnixNano < span.StartTimeUnixNano {
			t.Fatal("timestamps")
		}
	}
	if errors != 1 || roots != 4 || rt.report()["export_loss"] != uint64(0) || len(rt.traceCurrent) != 0 {
		t.Fatalf("errors=%d roots=%d report=%v", errors, roots, rt.report())
	}
}
func TestIndependentGoroutinesDoNotShareParentContext(t *testing.T) {
	rt, requests := tracedRuntime(t, nil)
	parent := StartTrace("parent")
	var wait sync.WaitGroup
	for range 4 {
		wait.Go(func() {
			defer FinishTrace(StartTrace("worker"))
			func() { defer FinishTrace(StartTrace("worker-child")) }()
		})
	}
	wait.Wait()
	FinishTrace(parent)
	rt.Close()
	spans := storedSpans(*requests)
	if len(spans) != 9 || len(*requests) != 5 {
		t.Fatalf("spans=%d roots=%d", len(spans), len(*requests))
	}
	for _, request := range *requests {
		tree := storedSpans([]*traceproto.ExportTraceServiceRequest{request})
		roots := 0
		for _, span := range tree {
			if len(span.ParentSpanId) == 0 {
				roots++
			}
		}
		if roots != 1 {
			t.Fatal("goroutines stitched together")
		}
	}
	if len(rt.traceCurrent) != 0 || rt.untrackedScopes != 0 {
		t.Fatal("goroutine context retained")
	}
}
func TestMetricAdmissionStaysStickyWhileGoTracingContinues(t *testing.T) {
	rt, requests := tracedRuntime(t, nil)
	scope := StartTrace("inflight")
	rt.enabled.Store(false)
	func() { defer FinishTrace(StartTrace("disabled")) }()
	FinishTrace(scope)
	func() { defer FinishTrace(StartTrace("root-disabled")) }()
	rt.Close()
	if rt.report()["function_calls"] != uint64(1) || len(storedSpans(*requests)) != 3 || len(*requests) != 2 {
		t.Fatal(rt.report(), len(storedSpans(*requests)))
	}
}
func TestRejectedGoRootSuppressesDescendantsWithoutAnUnboundedContextMap(t *testing.T) {
	rt, requests := tracedRuntime(t, func(p *policy.Plan) { p.Runtime.MaxFunctions = 1 })
	rt.functions["accepted"] = newFunction("accepted")
	func() {
		defer FinishTrace(StartTrace("rejected-root"))
		func() { defer FinishTrace(StartTrace("accepted")) }()
	}()
	func() { defer FinishTrace(StartTrace("accepted")) }()
	rt.Close()
	spans := storedSpans(*requests)
	if len(spans) != 1 || spans[0].Name != "accepted" || len(rt.traceCurrent) != 0 || rt.untrackedScopes != 0 {
		t.Fatal("partial tree, context leak or no recovery", rt.report())
	}
}
func TestGoroutineHeaderParserRejectsUnknownAndOverflowingIdentity(t *testing.T) {
	for _, header := range []string{"", "goroutine 0 [running]:", "goroutine x [running]:", "thread 1 [running]:", "goroutine 18446744073709551616 [running]:", "goroutine 1", "goroutine 1 other"} {
		if parseGoroutineHeader([]byte(header)) != 0 {
			t.Fatal("invalid identity accepted")
		}
	}
	if parseGoroutineHeader([]byte("goroutine 18446744073709551615 [running]:")) != ^uint64(0) || currentGoroutine() == 0 {
		t.Fatal("valid current identity rejected")
	}
}
func TestUnknownGoroutineIdentityDiscardsActiveTreesAndRecovers(t *testing.T) {
	rt, requests := tracedRuntime(t, nil)
	parent := StartTrace("parent")
	rt.goroutineIdentity = func() uint64 { return 0 }
	func() { defer FinishTrace(StartTrace("unknown-child")) }()
	rt.goroutineIdentity = currentGoroutine
	FinishTrace(parent)
	func() { defer FinishTrace(StartTrace("independent")) }()
	rt.Close()
	spans := storedSpans(*requests)
	if len(spans) != 1 || spans[0].Name != "independent" {
		t.Fatal("unknown context allowed a partial tree", len(spans))
	}
	if rt.untrackedScopes != 0 || len(rt.traceCurrent) != 0 {
		t.Fatal("unknown context retained bookkeeping")
	}
}
