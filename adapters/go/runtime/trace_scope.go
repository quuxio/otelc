package runtime

import (
	"math"
	goruntime "runtime"
	"strconv"
	"time"
)

// Scope contains primitive context metadata, never application values.
type Scope struct {
	rt                     *Runtime
	token, gid, previous   uint64
	hadPrevious, untracked bool
}

func parseGoroutineHeader(data []byte) uint64 {
	const prefix = "goroutine "
	if len(data) < len(prefix)+3 || string(data[:len(prefix)]) != prefix {
		return 0
	}
	end := len(prefix)
	for end < len(data) && data[end] >= '0' && data[end] <= '9' {
		end++
	}
	if end == len(prefix) || end+1 >= len(data) || data[end] != ' ' || data[end+1] != '[' {
		return 0
	}
	value, err := strconv.ParseUint(string(data[len(prefix):end]), 10, 64)
	if err != nil {
		return 0
	}
	return value
}
func currentGoroutine() uint64 {
	var header [64]byte
	n := goruntime.Stack(header[:], false)
	return parseGoroutineHeader(header[:n])
}
func StartTrace(name string) Scope {
	rt := active.Load()
	if rt == nil {
		return Scope{}
	}
	if rt.traces == nil {
		return Scope{rt: rt, token: rt.start(name)}
	}
	gid := rt.goroutineIdentity()
	rt.mu.Lock()
	defer rt.mu.Unlock()
	if rt.closed.Load() {
		return Scope{}
	}
	previous, hadPrevious := rt.traceCurrent[gid]
	scope := Scope{rt: rt, gid: gid, previous: previous, hadPrevious: hadPrevious}
	var parent *traceIdentity
	if hadPrevious {
		parent = rt.pending[previous].trace
		if parent == nil {
			parent = &traceIdentity{}
		}
	}
	suppress := func() {
		if hadPrevious {
			rt.traceCurrent[gid] = 0
		} else {
			scope.untracked = true
			if rt.untrackedScopes == math.MaxUint64 {
				rt.contextBlocked = true
			} else {
				rt.untrackedScopes++
			}
		}
	}
	reject := func(reason string) Scope {
		rt.losses[reason]++
		rt.revision.Add(1)
		if gid == 0 {
			rt.traces.rejectAll("context_identity")
		} else {
			rt.traces.reject(parent, reason)
		}
		suppress()
		return scope
	}
	if _, ok := rt.functions[name]; !ok {
		if len(rt.functions) >= rt.plan.Runtime.MaxFunctions || len(name) > 1024 {
			return reject("function_capacity")
		}
		rt.functions[name] = newFunction(name)
	}
	if len(rt.pending) >= rt.plan.Runtime.MaxActive {
		return reject("active_call_capacity")
	}
	if rt.next == math.MaxUint64 {
		return reject("invalid")
	}
	rt.next++
	scope.token = rt.next
	started := time.Now()
	identity := &traceIdentity{}
	if gid == 0 {
		rt.losses["invalid"]++
		rt.traces.rejectAll("context_identity")
		suppress()
	} else if !hadPrevious && (rt.untrackedScopes != 0 || rt.contextBlocked) {
		rt.traces.reject(nil, "context_capacity")
	} else {
		identity = rt.traces.begin(parent, name, started)
	}
	rt.pending[scope.token] = frame{name: name, start: started, metrics: rt.enabled.Load(), trace: identity}
	if gid != 0 {
		rt.traceCurrent[gid] = scope.token
	}
	return scope
}

// FinishTrace must remain the actual deferred function so direct recover works.
func FinishTrace(scope Scope) {
	if scope.rt == nil {
		return
	}
	if legacyNilPanic() {
		scope.rt.discardScope(scope)
		return
	}
	value := recover()
	scope.rt.finishScope(scope, value != nil, time.Now())
	if value != nil {
		panic(value)
	}
}
func (rt *Runtime) restoreScopeLocked(scope Scope) {
	if scope.untracked && rt.untrackedScopes > 0 {
		rt.untrackedScopes--
	}
	if scope.gid != 0 {
		if scope.hadPrevious {
			rt.traceCurrent[scope.gid] = scope.previous
		} else {
			delete(rt.traceCurrent, scope.gid)
		}
	}
}
func (rt *Runtime) finishScope(scope Scope, escaped bool, ended time.Time) {
	if rt.traces == nil {
		rt.finish(scope.token, escaped)
		return
	}
	rt.mu.Lock()
	defer rt.mu.Unlock()
	if rt.closed.Load() {
		return
	}
	value, exists := rt.pending[scope.token]
	if scope.token != 0 && !exists {
		return
	}
	rt.restoreScopeLocked(scope)
	if !exists {
		return
	}
	delete(rt.pending, scope.token)
	rt.traces.finish(value.trace, ended, escaped)
	rt.recordLocked(value, ended, escaped)
}
func (rt *Runtime) discardScope(scope Scope) {
	rt.mu.Lock()
	defer rt.mu.Unlock()
	if rt.closed.Load() {
		return
	}
	if value, exists := rt.pending[scope.token]; exists {
		rt.traces.reject(value.trace, "unsupported_runtime")
		delete(rt.pending, scope.token)
	}
	rt.restoreScopeLocked(scope)
	rt.losses["unsupported_runtime"]++
	rt.revision.Add(1)
}
