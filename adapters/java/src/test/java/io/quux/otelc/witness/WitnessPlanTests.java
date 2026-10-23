package io.quux.otelc.witness;

import static org.junit.jupiter.api.Assertions.*;

import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.nio.ByteBuffer;
import java.nio.channels.SeekableByteChannel;
import java.io.IOException;
import java.util.HashMap;
import java.util.Map;
import java.util.Properties;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class WitnessPlanTests {
  @TempDir Path root;

  public static void main(String[] arguments) throws Exception {
    try {
      WitnessPlan.load(Path.of(arguments[0]));
      throw new AssertionError("nonregular plan was accepted");
    } catch (IllegalArgumentException | IOException expected) {
      System.out.println("refused");
    }
  }

  @Test void fifoPlanIsRefusedBeforeOpeningItForReading() throws Exception {
    Path fifo = root.resolve("fifo.properties");
    Process create = new ProcessBuilder("mkfifo", fifo.toString()).start();
    assertTrue(create.waitFor(3, TimeUnit.SECONDS));
    assertEquals(0, create.exitValue());
    String executable = Path.of(System.getProperty("java.home"), "bin", "java").toString();
    Process child = new ProcessBuilder(executable, "-cp", System.getProperty("java.class.path"),
        WitnessPlanTests.class.getName(), fifo.toString()).start();
    try {
      assertTrue(child.waitFor(3, TimeUnit.SECONDS), "plan reader waited for a FIFO writer");
      assertEquals(0, child.exitValue());
      assertEquals("refused\n", new String(child.getInputStream().readAllBytes(), java.nio.charset.StandardCharsets.UTF_8));
      assertEquals(0, child.getErrorStream().readAllBytes().length);
    } finally {
      if (child.isAlive()) child.destroyForcibly().waitFor(3, TimeUnit.SECONDS);
    }
    assertThrows(IllegalArgumentException.class, () -> WitnessPlan.load(root));
  }

  @Test void openedFileGrowthCannotBypassTheReadBound() throws Exception {
    Path file = root.resolve("growing.properties");
    for (int size : new int[] {8, 32769}) {
      Files.writeString(file, "{}");
      try (var original = Files.newByteChannel(file, StandardOpenOption.READ)) {
        SeekableByteChannel growing = new SeekableByteChannel() {
          boolean changed;
          @Override public int read(ByteBuffer bytes) throws IOException {
            if (!changed) { changed = true; Files.writeString(file, "x".repeat(size)); }
            return original.read(bytes);
          }
          @Override public boolean isOpen() { return original.isOpen(); }
          @Override public void close() throws IOException { original.close(); }
          @Override public long size() throws IOException { return original.size(); }
          @Override public long position() throws IOException { return original.position(); }
          @Override public SeekableByteChannel position(long position) throws IOException { original.position(position); return this; }
          @Override public int write(ByteBuffer bytes) throws IOException { return original.write(bytes); }
          @Override public SeekableByteChannel truncate(long size) throws IOException { original.truncate(size); return this; }
        };
        if (size > 32768) {
          assertThrows(IllegalArgumentException.class, () -> WitnessPlan.boundedBytes(growing, 2));
          assertEquals(32769, original.position(), "read past the retained byte bound");
        } else {
          assertThrows(IOException.class, () -> WitnessPlan.boundedBytes(growing, 2));
        }
      }
    }
  }

  private Properties valid() throws Exception {
    Properties values = new Properties();
    values.setProperty("schema_version", "1");
    values.setProperty("profile", WitnessBindings.PROFILE);
    values.setProperty("socket", root.resolve("witness.sock").toString());
    for (String key : java.util.List.of("build", "selection", "policy", "tooling")) values.setProperty(key, "a".repeat(64));
    for (var binding : WitnessMethodGuardTests.bindings()) {
      String prefix = binding.role() + ".";
      values.setProperty(prefix + "owner", binding.declaration().owner());
      values.setProperty(prefix + "name", binding.declaration().name());
      values.setProperty(prefix + "descriptor", binding.declaration().descriptor());
      values.setProperty(prefix + "class_sha256", binding.classSha256());
      values.setProperty(prefix + "method_sha256", binding.methodSha256());
      values.setProperty(prefix + "probe_id", binding.probeId());
      values.setProperty(prefix + "origin", "file:/classes/");
    }
    return values;
  }

  private Path write(Properties values) throws Exception {
    Path file = root.resolve("plan.properties");
    try (var writer = Files.newBufferedWriter(file)) { values.store(writer, "generated witness plan"); }
    return file;
  }

  @Test void validatesClosedShapeAndAllCompilerIdentities() throws Exception {
    Properties values = valid();
    WitnessPlan plan = WitnessPlan.load(write(values));
    assertEquals(root.resolve("witness.sock"), plan.socket());
    assertEquals(4, plan.bindings().size());
    assertEquals(Map.of(WitnessMethodGuardTests.OWNER, "file:/classes/"), plan.origins());
    assertEquals("a".repeat(64), plan.build());
    assertEquals(plan.build(), plan.selection());
    assertEquals(plan.build(), plan.policy());
    assertEquals(plan.build(), plan.tooling());
    for (String mutation : java.util.List.of("schema_version", "profile", "unexpected", "target.probe_id", "target.origin", "target.owner", "tooling")) {
      Properties invalid = valid();
      invalid.setProperty(mutation, mutation.equals("target.probe_id") ? "0".repeat(64) : "invalid");
      Path file = write(invalid);
      assertThrows(IllegalArgumentException.class, () -> WitnessPlan.load(file), mutation);
    }
    for (String mutation : java.util.List.of("target.origin", "target.name")) {
      Properties invalid = valid(); invalid.remove(mutation);
      Path file = write(invalid);
      assertThrows(IllegalArgumentException.class, () -> WitnessPlan.load(file));
    }
    Path duplicate = write(values);
    Files.writeString(duplicate, Files.readString(duplicate) + "profile=other\n");
    assertThrows(IllegalArgumentException.class, () -> WitnessPlan.load(duplicate));
    Files.writeString(duplicate, "x".repeat(32769));
    assertThrows(IllegalArgumentException.class, () -> WitnessPlan.load(duplicate));
  }

  @Test void rejectsAliasedRolesAndConflictingOwnerOrigins() throws Exception {
    Properties values = valid();
    values.setProperty("target.origin", "file:/other/");
    Path file = write(values);
    assertThrows(IllegalArgumentException.class, () -> WitnessPlan.load(file));
    values = valid();
    Map<String, String> fields = new HashMap<>();
    for (String key : values.stringPropertyNames()) if (key.startsWith("wrapper.")) fields.put(key.substring(8), values.getProperty(key));
    for (var field : fields.entrySet()) values.setProperty("target." + field.getKey(), field.getValue());
    String expected = WitnessBindings.digest(String.join("\n", WitnessBindings.PROFILE, "target",
        values.getProperty("target.owner"), values.getProperty("target.name"), values.getProperty("target.descriptor"),
        values.getProperty("target.class_sha256"), values.getProperty("target.method_sha256"))
        .getBytes(java.nio.charset.StandardCharsets.UTF_8));
    values.setProperty("target.probe_id", expected);
    Path aliased = write(values);
    assertThrows(IllegalArgumentException.class, () -> WitnessPlan.load(aliased));
  }
}
