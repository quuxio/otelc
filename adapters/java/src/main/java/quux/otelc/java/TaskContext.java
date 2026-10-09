package quux.otelc.java;

import java.lang.ref.ReferenceQueue;
import java.lang.ref.WeakReference;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.FutureTask;

/** Immutable causal identities; weak identity keys never retain application tasks. */
final class TaskContext implements AutoCloseable {
  private static final class Key extends WeakReference<Object> {
    private final int hash;
    Key(Object task, ReferenceQueue<Object> queue) { super(task,queue); hash=System.identityHashCode(task); }
    @Override public int hashCode() { return hash; }
    @Override public boolean equals(Object other) { var task=get(); return this==other || task!=null && other instanceof Key key && task==key.get(); }
  }
  private record Entry(TraceStore.Identity parent, long lease) {}
  private record Scope(long previousCall, TraceStore.Identity previousParent, boolean previousUnknown) {}
  private final Telemetry runtime;
  private final ReferenceQueue<Object> collected=new ReferenceQueue<>();
  private final Map<Key,Entry> pending=new HashMap<>();
  private final ThreadLocal<TraceStore.Identity> current=new ThreadLocal<>();
  private boolean closed;
  private boolean lostContext;
  private final ThreadLocal<Boolean> unknown=new ThreadLocal<>();
  TaskContext(Telemetry runtime) { this.runtime=runtime; }
  TraceStore.Identity parent() { return current.get(); }
  private void reap() {
    Key key;
    while((key=(Key)collected.poll())!=null) {
      var entry=pending.remove(key);
      if(entry!=null) { runtime.traces.reject(entry.parent(),"context_incomplete"); runtime.traces.release(entry.lease()); }
    }
  }
  public void capture(Object task) {
    if(runtime.closed.get()) return;
    runtime.observations.lock();
    try {
      if(closed || runtime.closed.get()) return;
      reap(); var parent=runtime.parent();
      if(parent==null) return;
      var existing=pending.get(new Key(task,null));
      if(existing!=null) {
        if(!java.util.Objects.equals(existing.parent(),parent)) {lostContext=true;runtime.traces.reject(existing.parent(),"context_duplicate");runtime.traces.reject(parent,"context_duplicate");}
        return;
      }
      if(task.getClass()!=FutureTask.class) { runtime.traces.reject(parent,"context_unsupported"); return; }
      if(pending.size()>=runtime.plan.integer("runtime","max_active_calls")) { lostContext=true; runtime.traces.reject(parent,"context_capacity"); return; }
      pending.put(new Key(task,collected),new Entry(parent,runtime.traces.acquire(parent)));
    } finally {runtime.observations.unlock();}
  }
  public Object before(Object task) {
    if(runtime.closed.get()) return null;
    runtime.observations.lock();
    try {
      if(closed || runtime.closed.get()) return null;
      reap(); var entry=pending.get(new Key(task,null));
      if(entry==null && !lostContext) return null;
      var scope=new Scope(runtime.current.get(),current.get(),Boolean.TRUE.equals(unknown.get()));
      runtime.current.remove(); current.set(entry==null?TraceStore.SUPPRESSED:entry.parent());
      if(entry==null) unknown.set(true); else unknown.remove();
      return scope;
    } finally {runtime.observations.unlock();}
  }
  public void after(Object value) {
    var scope=(Scope)value;
    if(scope.previousCall()==0) runtime.current.remove(); else runtime.current.set(scope.previousCall());
    if(scope.previousUnknown()) unknown.set(true); else unknown.remove();
    if(scope.previousParent()==null) current.remove(); else current.set(scope.previousParent());
  }
  public void complete(Object task) {
    if(runtime.closed.get()) return;
    runtime.observations.lock();
    try {reap(); var entry=pending.remove(new Key(task,null)); if(entry!=null) runtime.traces.release(entry.lease());}
    finally {runtime.observations.unlock();}
  }
  public void finished(Object task) {
    if(runtime.closed.get()) return;
    runtime.observations.lock();
    try {if(pending.containsKey(new Key(task,null)) && ((FutureTask<?>)task).isDone()) complete(task);}
    finally {runtime.observations.unlock();}
  }
  void observed() {if(Boolean.TRUE.equals(unknown.get())) {unknown.remove();runtime.traces.reject(null,"context_untracked");}}
  public void failed() { if(runtime.closed.get()) return; runtime.observations.lock(); try {runtime.traces.reject(runtime.parent(),"context_hook");} finally {runtime.observations.unlock();} }
  int pending() { runtime.observations.lock(); try {reap();return pending.size();} finally {runtime.observations.unlock();} }
  @Override public void close() {
    runtime.observations.lock();
    try {closed=true; pending.clear(); current.remove(); unknown.remove(); TaskInstaller.unbind(this);}
    finally {runtime.observations.unlock();}
  }
}
