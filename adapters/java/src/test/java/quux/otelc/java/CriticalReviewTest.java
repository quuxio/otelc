package quux.otelc.java;
import static org.junit.jupiter.api.Assertions.*;
import io.opentelemetry.api.metrics.LongCounter;
import java.lang.reflect.Proxy;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.Executors;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.RepeatedTest;
class CriticalReviewTest {
 static Plan relaxedPlan(String endpoint) {
  var policy=AgentTest.policy(endpoint);policy.getAsJsonObject("export").addProperty("timeout_ms",1000);
  policy.getAsJsonObject("runtime").addProperty("shutdown_timeout_ms",2000);return new Plan(policy);
 }
 @Test void blockedSdkCollectionCannotHoldApplicationShutdown() throws Exception {
  try(var receiver=new AgentTest.Receiver();var rt=new Telemetry(relaxedPlan(receiver.endpoint()));var pool=Executors.newSingleThreadExecutor()){
   var reached=new CountDownLatch(1);var resume=new CountDownLatch(1);
   var field=Telemetry.class.getDeclaredField("provider");field.setAccessible(true);
   var provider=(io.opentelemetry.sdk.metrics.SdkMeterProvider)field.get(rt);
   try(var counter=provider.get("shutdown-regression").counterBuilder("test.blocked.collection").buildWithCallback(observer->{
    reached.countDown();try{resume.await();}catch(InterruptedException interrupted){Thread.currentThread().interrupt();}
    observer.record(1);
   })){
    rt.plan.section("runtime").addProperty("shutdown_timeout_ms",50);
    var closing=pool.submit(rt::close);
    try{
     assertTrue(reached.await(1,TimeUnit.SECONDS));
     closing.get(1,TimeUnit.SECONDS);
     assertEquals(false,rt.report().get("export_finished"));
     assertTrue(rt.exportLoss.get()>0);
     assertEquals(1L,resume.getCount(),"shutdown returned while the SDK callback remained blocked");
    }finally{resume.countDown();closing.get(3,TimeUnit.SECONDS);}
   }
  }
 }
 @RepeatedTest(10) void blockedRecordingCannotHoldShutdownOrCompleteLate() throws Exception {
  try(var receiver=new AgentTest.Receiver();var rt=new Telemetry(relaxedPlan(receiver.endpoint()));var pool=Executors.newFixedThreadPool(2)){
   var reached=new CountDownLatch(1);var resume=new CountDownLatch(1);
   var field=Telemetry.class.getDeclaredField("calls");field.setAccessible(true);
   var original=(LongCounter)field.get(rt);
   var intercepted=Proxy.newProxyInstance(LongCounter.class.getClassLoader(),new Class<?>[]{LongCounter.class},(proxy,method,args)->{
    if(method.getName().equals("add")){reached.countDown();resume.await();}
    return method.invoke(original,args);
   });
   field.set(rt,intercepted);
   long token=rt.enter("finishing");var done=pool.submit(()->rt.exit(token,false));
   java.util.concurrent.Future<?> closing=null;
   try{
    assertTrue(reached.await(1,TimeUnit.SECONDS));
    rt.plan.section("runtime").addProperty("shutdown_timeout_ms",50);
    long started=System.nanoTime();closing=pool.submit(rt::close);
    // Guard test progress, while the event assertions prove independence from recording.
    closing.get(1,TimeUnit.SECONDS);
    assertEquals(1L,resume.getCount(),"shutdown must return before the recording is released");
    assertFalse(done.isDone(),"the SDK recording must still be blocked");
    assertEquals(false,rt.report().get("export_finished"));
    assertTrue(rt.exportLoss.get()>0);
    assertEquals(1L,((java.util.Map<?,?>)rt.report().get("losses")).get("incomplete"));
    long exported=receiver.requests.stream().skip(Math.max(0,receiver.requests.size()-1)).flatMap(r->r.getResourceMetricsList().stream()).flatMap(r->r.getScopeMetricsList().stream()).flatMap(r->r.getMetricsList().stream()).filter(m->m.getName().equals("otelc.function.calls")).flatMap(m->m.getSum().getDataPointsList().stream()).mapToLong(p->p.getAsInt()).sum();
    System.out.println("Shutdown observation ns="+(System.nanoTime()-started)+"; FINAL report="+rt.report()+"; collector function calls="+exported);
    assertEquals(rt.count(),exported,"completed report and final SDK snapshot disagree; no loss is reported");
   }finally{resume.countDown();done.get(3,TimeUnit.SECONDS);if(closing!=null)closing.get(3,TimeUnit.SECONDS);}
   assertEquals(false,rt.report().get("export_finished"),"late recording must not complete shutdown");
   assertEquals(0,rt.count(),"late recording must not enter the completed report");
   assertEquals(1L,((java.util.Map<?,?>)rt.report().get("losses")).get("incomplete"));
  }
 }
 @Test void completionMustAgreeWithFinalSnapshotDuringClose() throws Exception {
  try(var receiver=new AgentTest.Receiver();var rt=new Telemetry(relaxedPlan(receiver.endpoint()));var pool=Executors.newFixedThreadPool(2)){
   var reached=new CountDownLatch(1);var resume=new CountDownLatch(1);
   var field=Telemetry.class.getDeclaredField("calls");field.setAccessible(true);
   var original=(LongCounter)field.get(rt);
   var intercepted=Proxy.newProxyInstance(LongCounter.class.getClassLoader(),new Class<?>[]{LongCounter.class},(proxy,method,args)->{
    if(method.getName().equals("add")){reached.countDown();if(!resume.await(2,TimeUnit.SECONDS))throw new AssertionError("barrier timeout");}
    return method.invoke(original,args);
   });
   field.set(rt,intercepted);
   long token=rt.enter("finishing");var done=pool.submit(()->rt.exit(token,false));
   assertTrue(reached.await(1,TimeUnit.SECONDS));
   try{
    var closing=pool.submit(rt::close);
    var closedField=Telemetry.class.getDeclaredField("closed");closedField.setAccessible(true);
    var closed=(java.util.concurrent.atomic.AtomicBoolean)closedField.get(rt);
    long deadline=System.nanoTime()+TimeUnit.SECONDS.toNanos(1);
    while(!closed.get()&&System.nanoTime()<deadline)Thread.onSpinWait();
    assertTrue(closed.get()); assertFalse(closing.isDone());
    resume.countDown(); done.get(1,TimeUnit.SECONDS); closing.get(3,TimeUnit.SECONDS);
    assertEquals(true,rt.report().get("export_finished"));
    assertEquals(0,rt.exportLoss.get()); assertEquals(1,rt.count());
    long exported=receiver.requests.stream().skip(Math.max(0,receiver.requests.size()-1)).flatMap(r->r.getResourceMetricsList().stream()).flatMap(r->r.getScopeMetricsList().stream()).flatMap(r->r.getMetricsList().stream()).filter(m->m.getName().equals("otelc.function.calls")).flatMap(m->m.getSum().getDataPointsList().stream()).mapToLong(p->p.getAsInt()).sum();
    System.out.println("FINAL report="+rt.report()+"; collector function calls="+exported);
    assertEquals(rt.count(),exported,"completed report and final SDK snapshot disagree; no loss is reported");
   }finally{resume.countDown();done.get(1,TimeUnit.SECONDS);}
  }
 }
}
