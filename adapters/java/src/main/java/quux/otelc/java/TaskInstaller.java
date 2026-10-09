package quux.otelc.java;

import java.lang.instrument.Instrumentation;
import java.nio.file.Files;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.FutureTask;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.jar.JarEntry;
import java.util.jar.JarFile;
import java.util.jar.JarOutputStream;

/** Only the dispatch bridge enters bootstrap search; SDK and application probes stay isolated. */
final class TaskInstaller {
  private TaskInstaller() {}
  static void bootstrap(Instrumentation instrumentation) throws Exception {
    if(!instrumentation.isRetransformClassesSupported() || !instrumentation.isModifiableClass(FutureTask.class)
      || !instrumentation.isModifiableClass(ThreadPoolExecutor.class)) throw new IllegalArgumentException("Java task context requires JDK FutureTask/ThreadPoolExecutor retransformation");
    var file=Files.createTempFile("otelc-java-task-bridge-",".jar"); file.toFile().deleteOnExit();
    try(var output=new JarOutputStream(Files.newOutputStream(file))) {
      for(String name:new String[]{"TaskBridge","TaskBridge$Hook","TaskBridge$Scope"}) {
        String entry="quux/otelc/bootstrap/"+name+".class";
        try(var input=TaskInstaller.class.getClassLoader().getResourceAsStream(entry)) {
          if(input==null) throw new IllegalArgumentException("Java task bootstrap bridge is missing");
          output.putNextEntry(new JarEntry(entry)); input.transferTo(output); output.closeEntry();
        }
      }
    }
    // The JVM owns this search entry for the lifetime of the process.
    instrumentation.appendToBootstrapClassLoaderSearch(new JarFile(file.toFile()));
    var bridge=Class.forName("quux.otelc.bootstrap.TaskBridge",true,null);
    instrumentation.redefineModule(FutureTask.class.getModule(),Set.of(bridge.getModule()),Map.of(),Map.of(),Set.of(),Map.of());
    var transformer=new TaskWeaver(); instrumentation.addTransformer(transformer,true);
    try {
      instrumentation.retransformClasses(FutureTask.class,ThreadPoolExecutor.class);
      if(!transformer.transformedPool || !transformer.transformedFuture) throw new IllegalArgumentException("Java task context transformation did not complete");
    } catch(Exception|Error failure) {instrumentation.removeTransformer(transformer);throw failure;}
  }
  static void bind(TaskContext context) throws Exception {
    Class.forName("quux.otelc.java.TaskBinding").getMethod("bind",Object.class).invoke(null,context);
  }
  static void unbind(TaskContext context) {
    try { Class.forName("quux.otelc.java.TaskBinding").getMethod("unbind",Object.class).invoke(null,context); }
    catch(ReflectiveOperationException failure) { throw new IllegalStateException("cannot detach Java task context",failure); }
  }
}
