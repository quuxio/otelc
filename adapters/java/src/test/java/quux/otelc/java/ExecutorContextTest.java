package quux.otelc.java;

import static org.junit.jupiter.api.Assertions.*;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class ExecutorContextTest {
  private Path testAgent() throws Exception {
    var filename=directory.resolve("agent.jar"); var manifest=new java.util.jar.Manifest();
    manifest.getMainAttributes().putValue("Manifest-Version","1.0");
    manifest.getMainAttributes().putValue("Premain-Class","quux.otelc.java.Agent");
    manifest.getMainAttributes().putValue("Can-Retransform-Classes","true");
    var classes=Path.of(Agent.class.getProtectionDomain().getCodeSource().getLocation().toURI());
    try(var output=new java.util.jar.JarOutputStream(Files.newOutputStream(filename),manifest); var files=Files.walk(classes)) {
      for(var file:files.filter(Files::isRegularFile).toList()) {
        String entry=classes.relativize(file).toString().replace(java.io.File.separatorChar,'/');
        output.putNextEntry(new java.util.jar.JarEntry(entry));Files.copy(file,output);output.closeEntry();
      }
    }
    return filename;
  }
  @TempDir Path directory;
  @Test void realAgentKeepsLateExecutorChildrenAndOriginalFutureTasks() throws Exception {
    var compiler = new AgentTest(); compiler.directory = directory;
    byte[] app=compiler.compile("WorkerApp", """
      package example;
      import java.util.concurrent.*;
      public class WorkerApp {
        static FutureTask<Integer> submitted;
        static int child(){return 42;}
        static FutureTask<Integer> root(ThreadPoolExecutor pool){
          submitted=new FutureTask<>(WorkerApp::child); pool.execute(submitted); return submitted;
        }
        public static void main(String[] args) throws Exception {
          var pool=(ThreadPoolExecutor)Executors.newFixedThreadPool(1);
          try {
            for(int i=0;i<2;i++) {
              var entered=new CountDownLatch(1); var gate=new CountDownLatch(1);
              var blocker=pool.submit(()->{entered.countDown();gate.await();return 0;});
              entered.await(); var result=root(pool); assert result==submitted;
              gate.countDown(); blocker.get(); assert result.get()==42;
            }
            System.out.println("workers=42,42; future-identity=true");
          } finally {pool.shutdownNow();}
        }
      }
      """);
    Files.createDirectories(directory.resolve("example"));
    Files.write(directory.resolve("example/WorkerApp.class"),app);
    try (var metrics = new AgentTest.Receiver(); var traces = new SpansTest.Receiver()) {
      var data = SpansTest.policy(metrics.endpoint(), traces.endpoint());
      data.add("propagation", com.google.gson.JsonParser.parseString("{\"tasks\":true,\"http\":false}"));
      data.getAsJsonObject("function_matchers").add("include", com.google.gson.JsonParser.parseString("[\"(?-u)example\\\\.WorkerApp\\\\.(root|child).*\"]"));
      var policy=directory.resolve("plan.json"); Files.writeString(policy,data.toString());
      var report=directory.resolve("report.json");
      String agent=System.getProperty("otelc.test.agent");
      if(agent==null) agent=testAgent().toString();
      var command=List.of(Path.of(System.getProperty("java.home"),"bin/java").toString(),"-Xshare:off","-ea","-javaagent:"+Path.of(agent).toAbsolutePath()+"="+policy,
        "-cp",directory+java.io.File.pathSeparator+System.getProperty("java.class.path"),"example.WorkerApp");
      var arguments=new java.util.ArrayList<>(command);
      for(var argument:java.lang.management.ManagementFactory.getRuntimeMXBean().getInputArguments()) {
        if(argument.startsWith("-javaagent:") && argument.contains("jacoco")) arguments.add(1,argument);
      }
      var builder=new ProcessBuilder(arguments); builder.environment().put("OTELC_REPORT_PATH",report.toString());
      builder.environment().keySet().removeIf(key->key.startsWith("OTEL_EXPORTER_OTLP"));
      var process=builder.start(); assertTrue(process.waitFor(15,java.util.concurrent.TimeUnit.SECONDS));
      assertEquals(0,process.exitValue(),new String(process.getErrorStream().readAllBytes()));
      assertEquals("workers=42,42; future-identity=true\n",new String(process.getInputStream().readAllBytes()));
      var spans=traces.spans(); assertEquals(4,spans.size());
      var roots=spans.stream().filter(span->span.getParentSpanId().isEmpty()).toList(); assertEquals(2,roots.size());
      for(var root:roots) {
        assertTrue(root.getName().contains(".root("));
        var children=spans.stream().filter(span->span.getParentSpanId().equals(root.getSpanId())).toList();
        assertEquals(1,children.size()); assertEquals(root.getTraceId(),children.getFirst().getTraceId());
        assertTrue(children.getFirst().getStartTimeUnixNano()>=root.getEndTimeUnixNano());
      }
      assertTrue(Files.exists(report));
    }
  }
  @Test void ordinaryExamplePreservesCancellationRejectionAndExceptionInstances() throws Exception {
    Path repository=Path.of("../..").toRealPath();
    Path source=repository.resolve("examples/apps/JavaWorkerApp.java");byte[] original=Files.readAllBytes(source);
    try(var metrics=new AgentTest.Receiver();var traces=new SpansTest.Receiver()) {
      var data=SpansTest.policy(metrics.endpoint(),traces.endpoint());
      data.add("propagation",com.google.gson.JsonParser.parseString("{\"tasks\":true}"));
      data.getAsJsonObject("source_matchers").add("include",com.google.gson.JsonParser.parseString("[\"(?-u)examples/apps/JavaWorkerApp\\\\.java\"]"));
      data.getAsJsonObject("function_matchers").add("include",com.google.gson.JsonParser.parseString("[\"(?-u)JavaWorkerApp\\\\.(root|child).*\"]"));
      Path policy=directory.resolve("plan.json");Files.writeString(policy,data.toString());
      Path report=directory.resolve("report.json");
      String executable=Path.of(System.getProperty("java.home"),"bin/java").toString();
      var plain=new ProcessBuilder(executable,"-Xshare:off",source.toString()).directory(repository.toFile()).start();
      assertTrue(plain.waitFor(15,java.util.concurrent.TimeUnit.SECONDS));
      assertEquals(0,plain.exitValue());byte[] output=plain.getInputStream().readAllBytes();
      assertEquals(0,plain.getErrorStream().readAllBytes().length);
      var arguments=new java.util.ArrayList<>(List.of(executable,"-Xshare:off","-javaagent:"+testAgent()+"="+policy,
        "-cp",System.getProperty("java.class.path"),source.toString()));
      for(var argument:java.lang.management.ManagementFactory.getRuntimeMXBean().getInputArguments()) {
        if(argument.startsWith("-javaagent:") && argument.contains("jacoco")) arguments.add(1,argument);
      }
      var builder=new ProcessBuilder(arguments).directory(repository.toFile());
      builder.environment().put("OTELC_REPORT_PATH",report.toString());
      builder.environment().keySet().removeIf(key->key.startsWith("OTEL_EXPORTER_OTLP"));
      var process=builder.start();assertTrue(process.waitFor(15,java.util.concurrent.TimeUnit.SECONDS));
      byte[] stderr=process.getErrorStream().readAllBytes();assertEquals(0,process.exitValue(),new String(stderr));
      assertArrayEquals(output,process.getInputStream().readAllBytes());assertEquals(0,stderr.length);
      var nodes=traces.spans();assertEquals(8,nodes.size());assertEquals(5,nodes.stream().filter(span->span.getParentSpanId().isEmpty()).count());
      assertEquals(2,nodes.stream().filter(span->span.getStatus().getCodeValue()==2).count());
      var runtime=com.google.gson.JsonParser.parseString(Files.readString(report)).getAsJsonObject();
      assertEquals(8,runtime.get("function_calls").getAsInt());assertEquals(0,runtime.get("export_loss").getAsInt());
      assertEquals(0,runtime.getAsJsonObject("traces").get("pending_contexts").getAsInt());
      assertTrue(runtime.getAsJsonObject("traces").getAsJsonObject("losses").isEmpty());
      assertArrayEquals(original,Files.readAllBytes(source));
    }
  }

}
