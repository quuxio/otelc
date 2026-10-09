package quux.otelc.java;

import static org.junit.jupiter.api.Assertions.*;

import java.lang.ref.WeakReference;
import java.util.concurrent.FutureTask;
import org.junit.jupiter.api.Test;

class TaskContextTest {
  private static final class Fixture implements AutoCloseable {
    final AgentTest.Receiver metrics=new AgentTest.Receiver();
    final SpansTest.Receiver spans=new SpansTest.Receiver();
    final Telemetry runtime;
    Fixture(int capacity,double ratio) throws Exception {
      var data=SpansTest.policy(metrics.endpoint(),spans.endpoint());
      data.getAsJsonObject("runtime").addProperty("max_active_calls",capacity);
      data.getAsJsonObject("traces").addProperty("root_sample_ratio",ratio);
      data.add("propagation",com.google.gson.JsonParser.parseString("{\"tasks\":true}"));
      runtime=new Telemetry(new Plan(data));
    }
    @Override public void close() {runtime.close(); spans.close(); metrics.close();}
  }
  @Test void completedParentRetainsCausalIdentityUntilWorkerFinishes() throws Exception {
    try(var fixture=new Fixture(8,1)) {
      var runtime=fixture.runtime; long parent=runtime.enter("parent");
      var original=new Object();
      var future=new FutureTask<>(()->{long child=runtime.enter("child"); runtime.exit(child,false); return original;});
      runtime.tasks.capture(future); runtime.exit(parent,false);
      assertEquals(1,runtime.tasks.pending()); assertEquals(0L,runtime.traces.report().get("completed_trees"));
      var scope=runtime.tasks.before(future); future.run(); runtime.tasks.after(scope); runtime.tasks.finished(future);
      assertSame(original,future.get()); assertNull(runtime.tasks.parent()); assertEquals(0,runtime.tasks.pending());
      runtime.close(); var spans=fixture.spans.spans(); assertEquals(2,spans.size());
      var root=spans.stream().filter(span->span.getParentSpanId().isEmpty()).findFirst().orElseThrow();
      assertEquals("parent",root.getName()); assertEquals(1,spans.stream().filter(span->span.getParentSpanId().equals(root.getSpanId())).count());
    }
  }
  @Test void cancelledQueuedSubmissionAndDuplicateCompletionReleaseExactlyOnce() throws Exception {
    try(var fixture=new Fixture(8,1)) {
      var runtime=fixture.runtime; long parent=runtime.enter("parent"); var task=new FutureTask<>(()->42);
      runtime.tasks.capture(task); runtime.tasks.capture(task); assertEquals(1,runtime.tasks.pending());
      runtime.exit(parent,false); assertTrue(task.cancel(false)); runtime.tasks.complete(task); runtime.tasks.complete(task);
      assertEquals(0,runtime.tasks.pending()); runtime.close(); assertEquals(1,fixture.spans.spans().size());
      assertEquals(0,runtime.traces.report().get("pending_contexts"));
    }
  }
  @Test void preCancelledFutureSubmittedLaterReleasesWhenRunReturns() throws Exception {
    try(var fixture=new Fixture(8,1)) {
      var runtime=fixture.runtime; var task=new FutureTask<>(()->42); assertTrue(task.cancel(false));
      long parent=runtime.enter("parent"); runtime.tasks.capture(task); runtime.exit(parent,false);
      var scope=runtime.tasks.before(task); task.run(); runtime.tasks.after(scope); runtime.tasks.finished(task);
      assertEquals(0,runtime.tasks.pending()); runtime.close(); assertEquals(1,fixture.spans.spans().size());
    }
  }
  @Test void repeatedRunCannotReleaseAStillRunningFuture() throws Exception {
    try(var fixture=new Fixture(8,1)) {
      var runtime=fixture.runtime; var started=new java.util.concurrent.CountDownLatch(1); var gate=new java.util.concurrent.CountDownLatch(1);
      var task=new FutureTask<>(()->{started.countDown();gate.await();return 42;});
      long parent=runtime.enter("parent");runtime.tasks.capture(task);runtime.exit(parent,false);
      var worker=new Thread(task);worker.start();assertTrue(started.await(5,java.util.concurrent.TimeUnit.SECONDS));
      try {task.run();runtime.tasks.finished(task);assertEquals(1,runtime.tasks.pending());}
      finally {gate.countDown();worker.join(5000);}
      runtime.tasks.finished(task);assertEquals(42,task.get());assertEquals(0,runtime.tasks.pending());
    }
  }
  @Test void workerContextRestoresExistingSelectedCallerAndErrorIdentity() throws Exception {
    try(var fixture=new Fixture(8,1)) {
      var runtime=fixture.runtime; var error=new IllegalArgumentException("private payload");
      long parent=runtime.enter("parent");
      var task=new FutureTask<>(()->{long child=runtime.enter("child"); runtime.exit(child,true);throw error;});
      runtime.tasks.capture(task);runtime.exit(parent,false);long independent=runtime.enter("independent");
      var scope=runtime.tasks.before(task); task.run(); runtime.tasks.after(scope);runtime.tasks.finished(task);
      assertEquals(independent,runtime.current.get()); assertNull(runtime.tasks.parent());
      assertSame(error,assertThrows(java.util.concurrent.ExecutionException.class,task::get).getCause());
      runtime.exit(independent,false);runtime.close();assertEquals(3,fixture.spans.spans().size());
      assertEquals(1,fixture.spans.spans().stream().filter(span->span.getStatus().getCodeValue()==2).count());
    }
  }
  @Test void capacityLossInvalidatesWholeTreeAndDoesNotResampleWorker() throws Exception {
    try(var fixture=new Fixture(1,1)) {
      var runtime=fixture.runtime;long parent=runtime.enter("parent");
      var one=new FutureTask<>(()->42);var two=new FutureTask<>(()->43);
      runtime.tasks.capture(one);runtime.tasks.capture(two);runtime.exit(parent,false);
      var scope=runtime.tasks.before(one);long child=runtime.enter("child");runtime.exit(child,false);runtime.tasks.after(scope);
      one.run();runtime.tasks.finished(one);
      var dropped=runtime.tasks.before(two);long untracked=runtime.enter("dropped-child");runtime.exit(untracked,false);if(dropped!=null)runtime.tasks.after(dropped);two.run();runtime.tasks.finished(two);
      runtime.close();assertTrue(fixture.spans.spans().isEmpty());
      assertEquals(1L,runtime.traces.losses().get("context_capacity"));
    }
  }
  @Test void unsampledCreatorIsNotResampledOnWorker() throws Exception {
    try(var fixture=new Fixture(8,0)) {
      var runtime=fixture.runtime;long parent=runtime.enter("parent");var task=new FutureTask<>(()->42);
      runtime.tasks.capture(task);runtime.exit(parent,false);var scope=runtime.tasks.before(task);
      long child=runtime.enter("child");runtime.exit(child,false);task.run();runtime.tasks.after(scope);runtime.tasks.finished(task);
      runtime.close();assertTrue(fixture.spans.spans().isEmpty());assertEquals(1L,runtime.traces.report().get("sampled_out_roots"));
    }
  }
  @Test void unqualifiedTasksReportLossAndAreNeverWrapped() throws Exception {
    try(var fixture=new Fixture(8,1)) {
      var runtime=fixture.runtime;long parent=runtime.enter("parent");Runnable original=()->{};
      runtime.tasks.capture(original);assertNull(runtime.tasks.before(original));runtime.tasks.complete(original);runtime.exit(parent,false);
      runtime.close();assertTrue(fixture.spans.spans().isEmpty());assertEquals(1L,runtime.traces.losses().get("context_unsupported"));
    }
  }
  @Test void shutdownCensorsPendingWorkAndStopsNewCapture() throws Exception {
    try(var fixture=new Fixture(8,1)) {
      var runtime=fixture.runtime;long parent=runtime.enter("parent");var task=new FutureTask<>(()->42);
      runtime.tasks.capture(task);runtime.exit(parent,false);runtime.close();assertTrue(fixture.spans.spans().isEmpty());
      assertEquals(1L,runtime.traces.losses().get("incomplete"));assertEquals(0,runtime.tasks.pending());
      runtime.tasks.capture(task);assertNull(runtime.tasks.before(task));runtime.tasks.complete(task);assertEquals(0,runtime.tasks.pending());
    }
  }
  @Test void registryDoesNotRetainFutureCallableOrPayload() throws Exception {
    try(var fixture=new Fixture(8,1)) {
      var runtime=fixture.runtime;long parent=runtime.enter("parent");
      var future=registeredWeakTask(runtime);runtime.exit(parent,false);
      for(int i=0;i<100 && future.get()!=null;i++){System.gc();Thread.sleep(10);}
      assertNull(future.get(),"bounded registry retained the Future and its callable/payload");
      assertEquals(0,runtime.tasks.pending());runtime.close();assertTrue(fixture.spans.spans().isEmpty());
      assertEquals(1L,runtime.traces.losses().get("context_incomplete"));
    }
  }
  private static WeakReference<FutureTask<Object>> registeredWeakTask(Telemetry runtime) {
    var payload=new Object();var task=new FutureTask<>(()->payload);runtime.tasks.capture(task);return new WeakReference<>(task);
  }
  @Test void duplicateContextLeaseReleaseAndHookFailureRemainVisible() throws Exception {
    try(var fixture=new Fixture(8,1)) {
      var runtime=fixture.runtime;runtime.traces.release(0);assertEquals(0L,runtime.traces.acquire(null));
      long parent=runtime.enter("parent");runtime.tasks.failed();runtime.exit(parent,false);runtime.close();
      assertEquals(1L,runtime.traces.losses().get("context_hook"));assertTrue(fixture.spans.spans().isEmpty());
    }
  }
  @Test void ambiguousTaskReuseInvalidatesBothParentsWithoutRetainingExtraTasks() throws Exception {
    try(var fixture=new Fixture(8,1)) {
      var runtime=fixture.runtime;var task=new FutureTask<>(()->42);
      long first=runtime.enter("first");runtime.tasks.capture(task);runtime.exit(first,false);
      long second=runtime.enter("second");runtime.tasks.capture(task);runtime.exit(second,false);
      assertEquals(1,runtime.tasks.pending());task.run();runtime.tasks.finished(task);runtime.close();
      assertTrue(fixture.spans.spans().isEmpty());assertEquals(2L,runtime.traces.losses().get("context_duplicate"));
    }
  }
  @Test void closedDispatchNeverWaitsOnTheRuntimeObservationLock() throws Exception {
    try(var fixture=new Fixture(8,1);var executor=java.util.concurrent.Executors.newSingleThreadExecutor()) {
      var runtime=fixture.runtime;runtime.observations.lock();runtime.closed.set(true);
      try {
        assertTrue(executor.submit(()->{
          var task=new FutureTask<>(()->42);runtime.tasks.capture(task);assertNull(runtime.tasks.before(task));
          runtime.tasks.complete(task);runtime.tasks.finished(task);runtime.tasks.failed();return true;
        }).get(100,java.util.concurrent.TimeUnit.MILLISECONDS));
      } finally {runtime.closed.set(false);runtime.observations.unlock();}
    }
  }
  @Test void policyRejectsTaskContextWithoutTraces() {
    var data=AgentTest.policy("http://127.0.0.1:1/v1/metrics");data.add("propagation",com.google.gson.JsonParser.parseString("{\"tasks\":true}"));
    assertThrows(IllegalArgumentException.class,()->new Plan(data));
  }
}
