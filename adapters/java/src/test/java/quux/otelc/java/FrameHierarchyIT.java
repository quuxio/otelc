package quux.otelc.java;

import static org.junit.jupiter.api.Assertions.*;
import com.google.gson.JsonParser;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import javax.tools.ToolProvider;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/** Real packaged agent qualification runs after Maven has created the JAR. */
class FrameHierarchyIT {
  @TempDir Path directory;
  @Test void inFlightClassHierarchyKeepsBothBranchOutcomesAndTelemetry() throws Exception {
    String source="""
      public class Main {
        static class Child extends Main {}
        Main selected(boolean flag) {
          Main value; if(flag)value=this;else value=new Child();return value;
        }
        public static void main(String[] args) {
          System.out.println(new Main().selected(Boolean.parseBoolean(args[0])).getClass().getSimpleName());
        }
      }
      """;
    Path file=directory.resolve("Main.java");Files.writeString(file,source);
    assertEquals(0,ToolProvider.getSystemJavaCompiler().run(null,null,null,"-g","-d",directory.toString(),file.toString()));
    Path agent=Path.of("target/java-agent-0.1.0-agent.jar").toAbsolutePath();
    try(var receiver=new AgentTest.Receiver()) {
      var policy=AgentTest.policy(receiver.endpoint());
      // This fixture qualifies class loading, not cold-JVM export deadlines.
      policy.getAsJsonObject("export").addProperty("timeout_ms",1000);
      policy.getAsJsonObject("runtime").addProperty("shutdown_timeout_ms",2000);
      policy.getAsJsonObject("function_matchers").getAsJsonArray("include").set(0,new com.google.gson.JsonPrimitive("(?-u)^Main\\.selected.*"));
      Path plan=directory.resolve("plan.json");Files.writeString(plan,policy.toString());
      for(String flag:List.of("true","false")) {
        for(boolean sourceLaunch:List.of(false,true)) {
          Path report=directory.resolve("report.json");
          var target=sourceLaunch?List.of("Main.java",flag):List.of("-cp",directory.toString(),"Main",flag);
          var plain=new java.util.ArrayList<String>();plain.add(Path.of(System.getProperty("java.home"),"bin/java").toString());plain.addAll(target);
          var original=new ProcessBuilder(plain).directory(directory.toFile()).redirectErrorStream(true).start();
          byte[] output=original.getInputStream().readAllBytes();assertEquals(0,original.waitFor());
          var measured=new java.util.ArrayList<String>();measured.add(plain.getFirst());measured.add("-javaagent:"+agent+"="+plan);measured.addAll(target);
          var builder=new ProcessBuilder(measured).directory(directory.toFile());builder.environment().put("OTELC_REPORT_PATH",report.toString());
          receiver.requests.clear();
          var process=builder.start();byte[] actual=process.getInputStream().readAllBytes();String errors=new String(process.getErrorStream().readAllBytes(),java.nio.charset.StandardCharsets.UTF_8);
          assertEquals(0,process.waitFor(),errors);assertArrayEquals(output,actual);assertEquals(source,Files.readString(file));
          var health=JsonParser.parseString(Files.readString(report)).getAsJsonObject();
          assertEquals(1,health.get("function_calls").getAsInt(),errors);
          String diagnostic="sourceLaunch="+sourceLaunch+", flag="+flag+", health="+health+", stderr="+errors;
          assertEquals(0,health.get("export_loss").getAsInt(),diagnostic);assertEquals("",errors);
          // OTLP counters are cumulative; force-flush and shutdown may both send a snapshot.
          var calls=receiver.requests.stream().flatMap(request -> request.getResourceMetricsList().stream())
            .flatMap(resource -> resource.getScopeMetricsList().stream()).flatMap(scope -> scope.getMetricsList().stream())
            .filter(metric -> metric.getName().equals("otelc.function.calls"))
            .flatMap(metric -> metric.getSum().getDataPointsList().stream())
            .filter(point -> point.getAttributesList().stream().anyMatch(attribute ->
              attribute.getKey().equals("code.function.name")&&attribute.getValue().getStringValue().startsWith("Main.selected(")))
            .map(point -> point.getAsInt()).toList();
          assertFalse(calls.isEmpty(),diagnostic);
          assertTrue(calls.stream().allMatch(count -> count==1),diagnostic+", wire counts="+calls);
        }
      }
    }
  }
}
