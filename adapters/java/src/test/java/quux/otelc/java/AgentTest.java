package quux.otelc.java;

import static org.junit.jupiter.api.Assertions.*;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.proto.collector.metrics.v1.ExportMetricsServiceRequest;
import io.opentelemetry.proto.collector.metrics.v1.ExportMetricsServiceResponse;
import io.opentelemetry.proto.collector.metrics.v1.ExportMetricsPartialSuccess;
import java.io.IOException;
import java.lang.reflect.InvocationTargetException;
import java.net.InetSocketAddress;
import java.net.StandardProtocolFamily;
import java.net.UnixDomainSocketAddress;
import java.nio.ByteBuffer;
import java.nio.channels.SocketChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFilePermissions;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.Executors;
import javax.tools.ToolProvider;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.objectweb.asm.ClassReader;
import org.objectweb.asm.tree.ClassNode;

class AgentTest {
  @TempDir Path directory;
  static JsonObject policy(String endpoint) {
    return JsonParser.parseString("""
      {"language":"java","execution_available":true,
       "source_matchers":{"include":["(?-u).*"],"exclude":[]},"function_matchers":{"include":["(?-u).*"],"exclude":["(?-u).*excluded.*"]},
       "annotations":{"read_existing":true,"inject_generated":false},
       "runtime":{"max_functions":64,"max_active_calls":64,"shutdown_timeout_ms":500,"control_socket":null},
       "metrics":{"enabled":true,"histogram_boundaries_seconds":[0.001,0.01,1]},
       "resource":{"service_name":"java-test","service_version":"1","attributes":{"test.attribute":"value"}},
       "export":{"timeout_ms":100,"interval_ms":60000},"metrics_endpoint":"ENDPOINT"}
      """.replace("ENDPOINT", endpoint)).getAsJsonObject();
  }
  static final class Receiver implements AutoCloseable {
    final HttpServer server;
    final List<ExportMetricsServiceRequest> requests = new CopyOnWriteArrayList<>();
    final int code;
    final byte[] response;
    Receiver() throws IOException { this(200, new byte[0]); }
    Receiver(int code, byte[] response) throws IOException {
      this.code = code; this.response = response;
      server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
      server.createContext("/v1/metrics", exchange -> {
        requests.add(ExportMetricsServiceRequest.parseFrom(exchange.getRequestBody().readAllBytes()));
        exchange.sendResponseHeaders(code, response.length == 0 ? -1 : response.length);
        if (response.length > 0) exchange.getResponseBody().write(response);
        exchange.close();
      });
      server.start();
    }
    String endpoint() { return "http://127.0.0.1:" + server.getAddress().getPort() + "/v1/metrics"; }
    @Override public void close() { server.stop(0); }
  }
  byte[] compile(String name, String source) throws IOException {
    var filename = directory.resolve(name + ".java"); Files.writeString(filename, source);
    var output = directory.resolve("classes"); Files.createDirectories(output);
    int status = ToolProvider.getSystemJavaCompiler().run(null, null, null, "-g", "-d", output.toString(), filename.toString());
    assertEquals(0, status);
    return Files.readAllBytes(output.resolve("example/" + name + ".class"));
  }
  static class Loader extends ClassLoader {
    Loader() { super(AgentTest.class.getClassLoader()); }
    Class<?> define(byte[] bytes) { return defineClass(null, bytes, 0, bytes.length); }
  }
  @Test void bytecodePreservesTypesThrowsRecursionAndConstructors() throws Exception {
    var source = """
      package example;
      public class App {
        public final int value;
        public App(int value) { if (value < 0) throw new IllegalStateException("constructor"); this.value=value; }
        public static int selected(int value) { if(value<0)throw new IllegalArgumentException("same");return value*2; }
        public static int recursive(int depth) { return depth==0?0:1+recursive(depth-1); }
        public static long wide(long value) { return value+1; }
        public static double floating(double value) { return value+1; }
        public static Object object(Object value) { return value; }
        public static int caught() { try{throw new Error("caught");}catch(Error error){return 7;} }
        public static void nothing() {}
        @OtelcExclude public static int excluded(){return 5;}
      }
      @interface OtelcExclude {}
      """;
    byte[] original = compile("App", source);
    try (var receiver = new Receiver(); var runtime = new Telemetry(new Plan(policy(receiver.endpoint())))) {
      Probes.runtime = runtime;
      var weaver = new Weaver(runtime.plan, new Sources(directory, runtime.plan), runtime, null);
      var loader = new Loader(); byte[] changed = weaver.weave(original, loader); assertNotNull(changed);
      Class<?> type = loader.define(changed);
      assertEquals(6, type.getMethod("selected", int.class).invoke(null, 3));
      var thrown = assertThrows(InvocationTargetException.class, () -> type.getMethod("selected", int.class).invoke(null, -1)); assertEquals("same", thrown.getCause().getMessage());
      assertEquals(3, type.getMethod("recursive", int.class).invoke(null, 3));
      assertEquals(9L, type.getMethod("wide", long.class).invoke(null, 8L)); assertEquals(9.5, type.getMethod("floating", double.class).invoke(null, 8.5));
      var value = new Object(); assertSame(value, type.getMethod("object", Object.class).invoke(null, value));
      assertEquals(7, type.getMethod("caught").invoke(null)); type.getMethod("nothing").invoke(null);
      var order = type.getConstructor(int.class).newInstance(8); assertEquals(8, type.getField("value").get(order));
      assertThrows(InvocationTargetException.class, () -> type.getConstructor(int.class).newInstance(-1));
      assertEquals(5, type.getMethod("excluded").invoke(null));
      assertEquals(13, runtime.count()); assertEquals(1, runtime.functions.get("example.App.selected(int)").unwinds.get()); assertEquals(1, runtime.functions.get("example.App.<init>(int)").unwinds.get());
      assertEquals(source, Files.readString(directory.resolve("App.java"))); assertThrows(IllegalArgumentException.class, () -> weaver.weave(changed, loader));
      runtime.close(); assertEquals(0, runtime.exportLoss.get()); assertEquals(true, runtime.report().get("export_finished"));
      assertFalse(receiver.requests.isEmpty());
      var metrics = receiver.requests.getLast().getResourceMetricsList().stream().flatMap(resource -> resource.getScopeMetricsList().stream()).flatMap(scope -> scope.getMetricsList().stream()).toList();
      assertEquals(13, metrics.stream().filter(metric -> metric.getName().equals("otelc.function.calls")).flatMap(metric -> metric.getSum().getDataPointsList().stream()).mapToLong(point -> point.getAsInt()).sum());
      assertEquals(13, metrics.stream().filter(metric -> metric.getName().equals("otelc.function.duration")).flatMap(metric -> metric.getHistogram().getDataPointsList().stream()).mapToLong(point -> point.getCount()).sum());
    } finally { Probes.runtime = null; }
  }
  @Test void annotationsExclusionsAndGeneratedMetadataAreOptional() throws Exception {
    byte[] original = compile("Annotated", """
      package example;
      public class Annotated {
        @OtelcInstrument public static int selected(){return 1;}
        @OtelcInstrument public static int excluded(){return 2;}
        public static int plain(){return 3;}
      }
      @interface OtelcInstrument {}
      """);
    var data = policy("http://127.0.0.1:1/v1/metrics"); data.getAsJsonObject("function_matchers").getAsJsonArray("include").set(0, new com.google.gson.JsonPrimitive("(?-u)never"));
    var p = new Plan(data); var sources = new Sources(directory, p); var weaver = new Weaver(p, sources, null, null);
    var node = new ClassNode(); new ClassReader(original).accept(node, 0);
    assertEquals(List.of("example.Annotated.selected()"), weaver.inventory(node).stream().filter(Weaver.Function::selected).map(Weaver.Function::name).toList());
    data.getAsJsonObject("annotations").addProperty("inject_generated", true); assertNotNull(weaver.weave(original, new Loader()));
    data.getAsJsonObject("annotations").addProperty("read_existing", false); assertNull(weaver.weave(original, new Loader()));
    assertNull(sources.name("example/Missing", "Missing.java")); assertNull(sources.name("example/Annotated", null));
  }
  @Test void liveControlCapacityAndAdmittedCallsRemainBounded() throws Exception {
    Files.setPosixFilePermissions(directory, PosixFilePermissions.fromString("rwx------"));
    var socket = directory.resolve("metrics.sock");
    try (var receiver = new Receiver()) {
      var data = policy(receiver.endpoint()); data.getAsJsonObject("runtime").addProperty("control_socket", socket.toString()); data.getAsJsonObject("runtime").addProperty("max_functions", 1); data.getAsJsonObject("runtime").addProperty("max_active_calls", 1);
      try (var runtime = new Telemetry(new Plan(data))) {
        runtime.bindControl(); assertEquals(PosixFilePermissions.fromString("rw-------"), Files.getPosixFilePermissions(socket));
        assertTrue(control(socket, "status\n").get("metrics_enabled").getAsBoolean());
        long token = runtime.enter("selected"); assertEquals(0, runtime.enter("selected")); assertEquals(0, runtime.enter("other"));
        assertFalse(control(socket, "disable\n").get("metrics_enabled").getAsBoolean()); assertEquals(0, runtime.enter("selected")); runtime.exit(token, true); assertEquals(1, runtime.count());
        assertTrue(control(socket, "enable\n").get("metrics_enabled").getAsBoolean());
        assertNotNull(control(socket, "invalid\n").get("error")); assertNotNull(control(socket, "incomplete").get("error")); assertNotNull(control(socket, "xxxxxxxxxxxxxxxxxxxx").get("error"));
        runtime.enter("selected"); assertEquals(1L, ((Map<?, ?>)runtime.report().get("losses")).get("incomplete"));
        runtime.close(); assertEquals(1, runtime.losses.get("incomplete").get()); assertEquals(1, runtime.losses.get("active_call_capacity").get()); assertEquals(1, runtime.losses.get("function_capacity").get()); assertFalse(Files.exists(socket));
      }
      Files.writeString(socket, "occupied"); try (var runtime = new Telemetry(new Plan(data))) { assertThrows(IOException.class, runtime::bindControl); assertEquals("occupied", Files.readString(socket)); }
      Files.delete(socket); Files.setPosixFilePermissions(directory, PosixFilePermissions.fromString("rwxr-xr-x")); try (var runtime = new Telemetry(new Plan(data))) { assertThrows(IOException.class, runtime::bindControl); }
    }
  }
  static JsonObject control(Path path, String request) throws IOException {
    try (var channel = SocketChannel.open(StandardProtocolFamily.UNIX)) {
      channel.connect(UnixDomainSocketAddress.of(path)); channel.write(ByteBuffer.wrap(request.getBytes(StandardCharsets.UTF_8)));
      var response = ByteBuffer.allocate(4096); while (channel.read(response) != -1) {}
      return JsonParser.parseString(new String(response.array(), 0, response.position(), StandardCharsets.UTF_8)).getAsJsonObject();
    }
  }
  @Test void strictTransportRejectsFailedPartialAndMalformedAcknowledgements() throws Exception {
    byte[] partial = ExportMetricsServiceResponse.newBuilder().setPartialSuccess(ExportMetricsPartialSuccess.newBuilder().setRejectedDataPoints(1)).build().toByteArray();
    for (var response : List.of(new byte[] {(byte)255}, partial, new byte[65537])) {
      try (var receiver = new Receiver(200, response); var runtime = new Telemetry(new Plan(policy(receiver.endpoint())))) { runtime.exit(runtime.enter("selected"), false); runtime.close(); assertTrue(runtime.exportLoss.get() >= 1); }
    }
    try (var receiver = new Receiver(503, new byte[0]); var runtime = new Telemetry(new Plan(policy(receiver.endpoint())))) { runtime.exit(runtime.enter("selected"), false); runtime.close(); assertTrue(runtime.exportLoss.get() >= 1); }
    try (var runtime = new Telemetry(new Plan(policy("http://127.0.0.1:1/v1/metrics")))) { runtime.exit(runtime.enter("selected"), false); runtime.close(); assertTrue(runtime.exportLoss.get() >= 1); }
    assertEquals(Map.of("x-a", "hello world"), Exporter.headers(Map.of("OTEL_EXPORTER_OTLP_HEADERS", "x-a=hello%20world")));
    assertEquals(Map.of("x-b", "new+value"), Exporter.headers(Map.of("OTEL_EXPORTER_OTLP_HEADERS", "x-a=old", "OTEL_EXPORTER_OTLP_METRICS_HEADERS", "x-b=new+value")));
    assertThrows(IllegalArgumentException.class, () -> Exporter.headers(Map.of("OTEL_EXPORTER_OTLP_HEADERS", "invalid")));
    assertThrows(IllegalArgumentException.class, () -> Exporter.headers(Map.of("OTEL_EXPORTER_OTLP_HEADERS", "x=%0d")));
  }
  @Test void policyAndSourceValidationUseByteGlobsAndOriginalPackages() throws Exception {
    var data = policy("http://127.0.0.1:1/v1/metrics"); var p = new Plan(data);
    assertFalse(p.functions.accepts("example.excluded()", true)); assertTrue(p.functions.accepts("hello\n", false));
    data.getAsJsonObject("function_matchers").getAsJsonArray("include").set(0, new com.google.gson.JsonPrimitive("(?-u).")); assertFalse(new Plan(data).functions.accepts("é", false));
    data.getAsJsonObject("function_matchers").getAsJsonArray("include").set(0, new com.google.gson.JsonPrimitive("(?-u)\\xc3\\xa9")); assertTrue(new Plan(data).functions.accepts("é", false));
    var invalid = policy("http://127.0.0.1:1"); invalid.addProperty("language", "python"); assertThrows(IllegalArgumentException.class, () -> new Plan(invalid));
    var file = directory.resolve("plan.json"); Files.writeString(file, policy("http://127.0.0.1:1").toString()); assertNotNull(new Plan(file));
    assertNull(Probes.runtime); assertEquals(0, Probes.enter("none")); Probes.exit(0, false);
    Files.writeString(directory.resolve("Bad.java"), "package example; public class Bad {"); assertThrows(IllegalArgumentException.class, () -> new Sources(directory, p));
  }
  @Test void entrypointsTransformModulesAndRejectUnsafeLoaders() throws Exception {
    byte[] original = compile("Entry", "package example; public class Entry { public static Object selected(boolean value) { return value ? new java.util.ArrayList<>() : new java.util.LinkedList<>(); } }");
    try (var receiver = new Receiver(); var runtime = new Telemetry(new Plan(policy(receiver.endpoint())))) {
      var events = new ArrayList<String>();
      var instrumentation = (java.lang.instrument.Instrumentation) java.lang.reflect.Proxy.newProxyInstance(getClass().getClassLoader(), new Class<?>[]{java.lang.instrument.Instrumentation.class}, (proxy, method, args) -> { events.add(method.getName()); return null; });
      var sources = new Sources(directory, runtime.plan);
      var weaver = new Weaver(runtime.plan, sources, runtime, instrumentation);
      assertNull(weaver.transform(null, getClass().getClassLoader(), null, null, null, original));
      assertNull(weaver.transform(null, getClass().getClassLoader(), "quux/otelc/java/Agent", null, null, original));
      assertNotNull(weaver.transform(null, getClass().getClassLoader(), "quux/otelc/application/Entry", null, null, original));
      assertNull(weaver.transform(null, getClass().getClassLoader(), "example/Entry", String.class, null, original));
      assertNotNull(weaver.transform(String.class.getModule(), getClass().getClassLoader(), "example/Entry", null, null, original));
      assertTrue(events.contains("redefineModule"));
      assertNull(weaver.transform(null, new ClassLoader(null){}, "example/Entry", null, null, original));
      assertEquals(1, runtime.losses.get("unsupported_class").get());
      assertNull(weaver.transform(null, getClass().getClassLoader(), "broken", null, null, new byte[0]));
      var planFile = directory.resolve("policy.json"); Files.writeString(planFile, policy(receiver.endpoint()).toString());
      Agent.main(new String[]{planFile.toString(), "--doctor"});
      Agent.main(new String[]{planFile.toString(), "--inspect", directory.resolve("classes/example/Entry.class").toString(), "--json"});
      assertThrows(IllegalArgumentException.class, () -> Agent.main(new String[]{}));
      assertThrows(IllegalArgumentException.class, () -> Agent.main(new String[]{planFile.toString(), "bad"}));
      Agent.premain(planFile.toString(), instrumentation);
      assertTrue(events.contains("addTransformer")); assertNotNull(Probes.runtime);
      assertThrows(IllegalArgumentException.class, () -> Agent.premain(planFile.toString(), instrumentation));
      Probes.runtime.close(); Probes.runtime = null;
      var empty = new Sources(directory.resolve("classes"), runtime.plan); assertNull(empty.name("example/Entry", "Entry.java"));
      Files.createDirectories(directory.resolve("duplicate")); Files.copy(directory.resolve("Entry.java"), directory.resolve("duplicate/Entry.java"));
      assertThrows(IllegalArgumentException.class, () -> new Sources(directory, runtime.plan));
    } finally { if (Probes.runtime != null) { Probes.runtime.close(); Probes.runtime = null; } }
  }
}
