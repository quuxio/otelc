package quux.otelc.java;

import static org.junit.jupiter.api.Assertions.*;

import org.junit.jupiter.api.Test;
import quux.otelc.bootstrap.TaskBridge;

class TaskBridgeTest {
  @Test void absentDispatchIsANoopAndInstalledDispatchPreservesIdentity() {
    var previous=TaskBridge.hook;var task=new Object();var scope=new Object();var calls=new java.util.ArrayList<String>();
    try {
      TaskBridge.hook=null;TaskBridge.capture(task);assertNull(TaskBridge.before(task));TaskBridge.after(scope);TaskBridge.complete(task);TaskBridge.finished(task);
      TaskBridge.hook=new TaskBridge.Hook() {
        @Override public void capture(Object value) {assertSame(task,value);calls.add("capture");}
        @Override public Object before(Object value) {assertSame(task,value);calls.add("before");return scope;}
        @Override public void after(Object value) {assertSame(scope,value);calls.add("after");}
        @Override public void complete(Object value) {assertSame(task,value);calls.add("complete");}
        @Override public void finished(Object value) {assertSame(task,value);calls.add("finished");}
        @Override public void failed() {fail("unexpected probe failure");}
      };
      TaskBridge.capture(task);var captured=TaskBridge.before(task);assertNotNull(captured);TaskBridge.after(null);TaskBridge.after(captured);TaskBridge.complete(task);TaskBridge.finished(task);
      assertEquals(java.util.List.of("capture","before","after","complete","finished"),calls);
      assertThrows(IllegalArgumentException.class,()->TaskBinding.bind(new Object()));
      TaskBinding.unbind(new Object());assertNotNull(TaskBridge.hook);
    } finally {TaskBridge.hook=previous;}
  }
  @Test void probeFailuresCannotReplaceApplicationResultsOrThrowables() {
    var previous=TaskBridge.hook;var failures=new java.util.concurrent.atomic.AtomicInteger();
    try {
      TaskBridge.hook=new TaskBridge.Hook() {
        @Override public void capture(Object value) {throw new IllegalStateException("probe");}
        @Override public Object before(Object value) {throw new IllegalStateException("probe");}
        @Override public void after(Object value) {throw new IllegalStateException("probe");}
        @Override public void complete(Object value) {throw new IllegalStateException("probe");}
        @Override public void finished(Object value) {throw new IllegalStateException("probe");}
        @Override public void failed() {failures.incrementAndGet();throw new IllegalStateException("failed diagnostic");}
      };
      assertDoesNotThrow(()->TaskBridge.capture(new Object()));assertNull(TaskBridge.before(new Object()));
      assertDoesNotThrow(()->TaskBridge.after(new Object()));assertDoesNotThrow(()->TaskBridge.complete(new Object()));assertDoesNotThrow(()->TaskBridge.finished(new Object()));
      assertEquals(4,failures.get());
    } finally {TaskBridge.hook=previous;}
  }
  @Test void taskWeaverLeavesUnrelatedBootstrapClassesUntouched() throws Exception {
    try(var stream=Object.class.getResourceAsStream("Object.class")) {
      var weaver=new TaskWeaver();assertNull(weaver.weave("java/lang/Object",stream.readAllBytes()));
      assertNull(weaver.transform(null,null,null,null,null,new byte[0]));
    }
  }
  @Test void capturedDispatchOwnerRestoresScopeAfterHookDetachment() {
    var previous=TaskBridge.hook;var restored=new java.util.concurrent.atomic.AtomicBoolean();var expected=new Object();
    try {
      TaskBridge.hook=new TaskBridge.Hook() {
        @Override public void capture(Object task) { /* Only exit ownership is exercised here. */ }
        @Override public Object before(Object task) {return expected;}
        @Override public void after(Object scope) {assertSame(expected,scope);restored.set(true);}
        @Override public void complete(Object task) { /* No task registry is needed for dispatch ownership. */ }
        @Override public void finished(Object task) { /* No task registry is needed for dispatch ownership. */ }
        @Override public void failed() {fail("unexpected dispatch failure");}
      };
      var scope=TaskBridge.before(new Object());TaskBridge.hook=null;TaskBridge.after(scope);
      assertTrue(restored.get(),"detaching the launch hook skipped the running worker's exit scope");
    } finally {TaskBridge.hook=previous;}
  }

  @Test void ownedExitFailureRemainsBoundedAfterGlobalDispatchChanges() {
    var previous=TaskBridge.hook;var failures=new java.util.concurrent.atomic.AtomicInteger();
    try {
      TaskBridge.hook=new TaskBridge.Hook() {
        @Override public void capture(Object task) { /* Only an exit failure is exercised. */ }
        @Override public Object before(Object task) {return new Object();}
        @Override public void after(Object scope) {throw new IllegalStateException("probe exit");}
        @Override public void complete(Object task) { /* No task registry is needed for dispatch ownership. */ }
        @Override public void finished(Object task) { /* No task registry is needed for dispatch ownership. */ }
        @Override public void failed() {failures.incrementAndGet();throw new IllegalStateException("diagnostic failure");}
      };
      var scope=TaskBridge.before(new Object());TaskBridge.hook=null;
      assertDoesNotThrow(()->TaskBridge.after(scope));assertEquals(1,failures.get());
    } finally {TaskBridge.hook=previous;}
  }

}
