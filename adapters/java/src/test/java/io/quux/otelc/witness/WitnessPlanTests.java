package io.quux.otelc.witness;

import static org.junit.jupiter.api.Assertions.*;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.Map;
import java.util.Properties;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class WitnessPlanTests {
  @TempDir Path root;

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
