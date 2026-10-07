package quux.otelc.java;

import com.google.gson.GsonBuilder;
import java.lang.instrument.Instrumentation;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import org.objectweb.asm.ClassReader;
import org.objectweb.asm.tree.ClassNode;

/** External -javaagent launch adapter; no application imports or source rewriting. */
public final class Agent {
  private Agent() {}
  public static void premain(String filename, Instrumentation instrumentation) throws Exception {
    if (Probes.runtime != null) throw new IllegalArgumentException("Java instrumentation is already installed");
    var plan = new Plan(Path.of(filename));
    var sources = new Sources(Path.of("").toRealPath(), plan);
    var runtime = new Telemetry(plan);
    try { runtime.bindControl(); }
    catch (Exception failure) { runtime.close(); throw failure; }
    Probes.runtime = runtime;
    instrumentation.addTransformer(new Weaver(plan, sources, runtime, instrumentation), false);
    java.lang.Runtime.getRuntime().addShutdownHook(new Thread(runtime::close, "otelc-java-shutdown"));
  }
  public static void main(String[] args) throws Exception {
    if (args.length < 2) throw new IllegalArgumentException("Java doctor or inspect requires a resolved plan");
    var plan = new Plan(Path.of(args[0]));
    var sources = new Sources(Path.of("").toRealPath(), plan);
    if (args[1].equals("--doctor") && args.length == 2) {
      System.out.println("Java " + java.lang.Runtime.version() + ": bytecode function probes and OTLP/HTTP metrics available");
    } else if (args[1].equals("--inspect") && args.length >= 3 && java.util.Arrays.stream(args).skip(3).allMatch(value -> value.equals("--json"))) {
      var node = new ClassNode(); new ClassReader(Files.readAllBytes(Path.of(args[2]))).accept(node, ClassReader.SKIP_CODE);
      var functions = new Weaver(plan, sources, null, null).inventory(node);
      System.out.println(new GsonBuilder().setPrettyPrinting().create().toJson(Map.of("language", "java", "functions", functions)));
    } else throw new IllegalArgumentException("Java inspect requires CLASSFILE [--json]");
  }
}
