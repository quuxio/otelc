package io.quux.otelc.witness;

import static org.junit.jupiter.api.Assertions.*;

import io.opentelemetry.javaagent.bootstrap.InstrumentationHolder;
import io.quux.otelc.tools.JarInventory;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Properties;
import java.util.concurrent.TimeUnit;
import java.util.jar.JarEntry;
import java.util.jar.JarOutputStream;
import java.util.jar.JarFile;
import javax.tools.ToolProvider;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class WitnessClassAgentTests {
  @TempDir Path root;

  private static final String APPLICATION = """
      package witness;
      public class App {
        static {
          try { java.nio.file.Files.writeString(java.nio.file.Path.of(System.getProperty("otelc.test.marker")), "initialised"); }
          catch (java.io.IOException error) { throw new java.io.UncheckedIOException(error); }
        }
        static int wrapper(int value) { return Work.target(value); }
        static void rendezvous() { Thread.onSpinWait(); }
        static int envelope(int value) { rendezvous(); return wrapper(value); }
        public static void main(String[] args) { System.out.println(envelope(7)); }
      }
      final class Work { static int target(int value) { return value + 1; } }
      """;

  @Test void actualAgentPreservesOriginalCaptureBeforeInstallingMethodProbes() throws Exception {
    Path classes = compile();
    Path extension = extension();
    Result plain = launch(classes, extension, "plain");
    assertEquals("8\n", plain.stdout());
    for (String mode : java.util.List.of("unselected", "selected", "jar", "tamper", "origin", "retransform", "preloaded")) {
      Result actual = launch(classes, extension, mode);
      assertEquals(plain.stdout(), actual.stdout(), actual.stderr());
      assertEquals("false", actual.report().getProperty("initialised_before_main"), actual.stderr());
      if (java.util.Set.of("selected", "unselected", "jar").contains(mode)) {
        assertNull(actual.report().getProperty("rejected"), actual.report() + "\n" + actual.stderr());
        assertEquals(actual.report().getProperty("expected_class"), actual.report().getProperty("captured_class"));
        assertEquals("true", actual.report().getProperty("loaded_identity"));
        Path classpath = mode.equals("jar") ? root.resolve("application.jar").toRealPath() : classes;
        assertEquals(classpath.toUri().toURL().toExternalForm(), actual.report().getProperty("loaded_origin"));
        String expected = actual.report().getProperty("expected_method");
        String downstream = actual.report().getProperty("downstream_method");
        assertNotNull(downstream);
        if (mode.equals("unselected")) assertEquals(expected, downstream, "unselected method remains unchanged");
        else assertNotEquals(expected, downstream, "actual SDK advice must follow raw capture");
      } else {
        assertNotNull(actual.report().getProperty("rejected"), mode + ": " + actual.stderr());
        assertNull(actual.report().getProperty("captured_class"), "rejection must not publish valid evidence");
      }
    }
  }

  @Test void runtimeArchiveMembersBindOriginalCaptureAndRejectStaleBaseBindings() throws Exception {
    Path base = compile();
    Path current = Files.createDirectory(root.resolve("current"));
    Path source = root.resolve("App.java");
    Files.writeString(source, APPLICATION.replace("value + 1", "value + 2"));
    assertEquals(0, ToolProvider.getSystemJavaCompiler().run(null, null, null,
        "--release", "21", "-d", current.toString(), source.toString()));
    Path extension = extension();
    for (String kind : java.util.List.of("active", "inactive", "folded")) {
      Path archive = root.resolve(kind + ".jar");
      String flag = switch (kind) {
        case "active" -> "true\r\n";
        case "inactive" -> "false\r\n";
        default -> "tr\r\n ue\r\n";
      };
      try (var output = new JarOutputStream(Files.newOutputStream(archive))) {
        output.putNextEntry(new JarEntry("META-INF/MANIFEST.MF"));
        output.write(("Manifest-Version: 1.0\r\nMulti-Release: " + flag + "\r\n")
            .getBytes(java.nio.charset.StandardCharsets.UTF_8));
        output.closeEntry();
        for (String name : java.util.List.of("witness/App.class", "witness/Work.class")) {
          output.putNextEntry(new JarEntry(name));
          Files.copy(base.resolve(name), output); output.closeEntry();
        }
        output.putNextEntry(new JarEntry("META-INF/versions/21/witness/Work.class"));
        Files.copy(current.resolve("witness/Work.class"), output); output.closeEntry();
      }
      var inventory = JarInventory.inspect(archive);
      Path view = Files.createDirectory(root.resolve(kind + "-metadata"));
      try (var jar = new JarFile(archive.toFile(), true, java.util.zip.ZipFile.OPEN_READ, Runtime.version())) {
        for (var member : inventory.classes().entrySet()) {
          String logical = member.getKey().replace('.', '/') + ".class";
          var entry = jar.getJarEntry(logical);
          assertEquals(member.getValue().entry(), entry.getRealName());
          byte[] bytes;
          try (var input = jar.getInputStream(entry)) { bytes = input.readAllBytes(); }
          assertEquals(member.getValue().sha256(), WitnessBindings.digest(bytes));
          Path target = view.resolve(logical);
          Files.createDirectories(target.getParent()); Files.write(target, bytes);
        }
      }
      Result plain = launch(view, extension, "plain-" + kind, archive);
      Result actual = launch(view, extension, "mr-" + kind, archive);
      assertEquals(inventory.multiRelease() ? "9\n" : "8\n", plain.stdout());
      assertEquals(plain.stdout(), actual.stdout(), actual.stderr());
      assertNull(actual.report().getProperty("rejected"), actual.report() + "\n" + actual.stderr());
      assertEquals("false", actual.report().getProperty("initialised_before_main"));
      assertEquals("true", actual.report().getProperty("loaded_identity"));
      assertEquals(archive.toRealPath().toUri().toURL().toExternalForm(), actual.report().getProperty("loaded_origin"));
      assertEquals(inventory.classes().get("witness.App").sha256(), actual.report().getProperty("captured_class"));
      assertNotEquals(actual.report().getProperty("expected_method"), actual.report().getProperty("downstream_method"));
      if (inventory.multiRelease()) {
        Result stale = launch(base, extension, "stale-" + kind, archive);
        assertNotNull(stale.report().getProperty("rejected"));
        assertNull(stale.report().getProperty("captured_class"));
        assertEquals(plain.stdout(), stale.stdout());
      }
    }
  }

  private Path compile() throws IOException {
    Path classes = Files.createDirectory(root.resolve("classes"));
    Path source = root.resolve("App.java");
    Files.writeString(source, APPLICATION);
    assertEquals(0, ToolProvider.getSystemJavaCompiler().run(null, null, null,
        "--release", "21", "-d", classes.toString(), source.toString()));
    try (var output = new JarOutputStream(Files.newOutputStream(root.resolve("application.jar")))) {
      for (String name : java.util.List.of("witness/App.class", "witness/Work.class")) {
        output.putNextEntry(new JarEntry(name));
        Files.copy(classes.resolve(name), output);
        output.closeEntry();
      }
    }
    return classes.toRealPath();
  }

  private Path extension() throws Exception {
    Path jar = root.resolve("capture-extension.jar");
    Path main = Path.of(WitnessClassCapture.class.getProtectionDomain().getCodeSource().getLocation().toURI());
    Path tests = Path.of(WitnessCaptureTestExtension.class.getProtectionDomain().getCodeSource().getLocation().toURI());
    try (var output = new JarOutputStream(Files.newOutputStream(jar))) {
      for (Path tree : java.util.List.of(main, tests)) {
        try (var paths = Files.walk(tree.resolve("io/quux/otelc/witness"))) {
          for (Path path : paths.filter(Files::isRegularFile).toList()) {
            String name = tree.relativize(path).toString().replace(java.io.File.separatorChar, '/');
            if (tree.equals(tests) && !name.contains("WitnessCaptureTestExtension")) continue;
            output.putNextEntry(new JarEntry(name));
            Files.copy(path, output);
            output.closeEntry();
          }
        }
      }
      for (String service : java.util.List.of(AgentServices.CUSTOMIZER, AgentServices.LISTENER)) {
        output.putNextEntry(new JarEntry("META-INF/services/" + service));
        output.write((WitnessCaptureTestExtension.class.getName() + "\n").getBytes(java.nio.charset.StandardCharsets.UTF_8));
        output.closeEntry();
      }
    }
    return jar;
  }

  private static final class AgentServices {
    static final String CUSTOMIZER = "io.opentelemetry.sdk.autoconfigure.spi.AutoConfigurationCustomizerProvider";
    static final String LISTENER = "io.opentelemetry.javaagent.extension.AgentListener";
    private AgentServices() {}
  }

  private record Result(String stdout, String stderr, Properties report) {}

  private Result launch(Path classes, Path extension, String mode) throws Exception {
    Path classpath = mode.equals("jar") ? root.resolve("application.jar").toRealPath() : classes;
    return launch(classes, extension, mode, classpath);
  }

  private Result launch(Path classes, Path extension, String mode, Path classpath) throws Exception {
    Path directory = Files.createDirectory(root.resolve(mode));
    Path marker = directory.resolve("initialised");
    Path resolvedClasspath = classpath.toRealPath();
    var command = new ArrayList<>(java.util.List.of(Path.of(System.getProperty("java.home"), "bin/java").toString(),
        "-Dotelc.test.marker=" + marker));
    if (!mode.startsWith("plain")) {
      Path agent = Path.of(InstrumentationHolder.class.getProtectionDomain().getCodeSource().getLocation().toURI());
      command.addAll(java.util.List.of("-javaagent:" + agent, "-Dotel.javaagent.extensions=" + extension,
          "-Dotelc.test.classes=" + classes, "-Dotelc.test.classpath=" + resolvedClasspath, "-Dotelc.test.mode=" + mode,
          "-Dotelc.test.report=" + directory.resolve("report.properties"),
          "-Dotel.instrumentation.common.default-enabled=false",
          "-Dotel.instrumentation.methods.enabled=true",
          "-Dotel.instrumentation.methods.include=witness.Work[" + (mode.equals("unselected") ? "absent" : "target") + "]",
          "-Dotel.metrics.exporter=none", "-Dotel.traces.exporter=none", "-Dotel.logs.exporter=none"));
    }
    command.addAll(java.util.List.of("-cp", resolvedClasspath.toString(), "witness.App"));
    ProcessBuilder builder = new ProcessBuilder(command).redirectOutput(directory.resolve("stdout").toFile())
        .redirectError(directory.resolve("stderr").toFile());
    // Ambient agent/launcher options cannot alter this exact pinned-agent experiment.
    for (String variable : new ArrayList<>(builder.environment().keySet())) {
      if (variable.startsWith("OTEL_") || java.util.Set.of("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS").contains(variable)) {
        builder.environment().remove(variable);
      }
    }
    Process process = builder.start();
    try {
      assertTrue(process.waitFor(20, TimeUnit.SECONDS), mode + " timed out");
      String stderr = Files.readString(directory.resolve("stderr"));
      assertEquals(0, process.exitValue(), stderr);
      assertEquals("initialised", Files.readString(marker));
      Properties report = new Properties();
      if (!mode.startsWith("plain")) {
        try (var input = Files.newInputStream(directory.resolve("report.properties"))) { report.load(input); }
      }
      return new Result(Files.readString(directory.resolve("stdout")), stderr, report);
    } finally {
      if (process.isAlive()) process.destroyForcibly().waitFor(5, TimeUnit.SECONDS);
    }
  }
}
