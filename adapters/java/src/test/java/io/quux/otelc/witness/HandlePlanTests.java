package io.quux.otelc.witness;

import static org.junit.jupiter.api.Assertions.*;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import java.util.Properties;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.objectweb.asm.Type;

class HandlePlanTests {
  @Test void parsesOnlyTheExplicitClosedPlanAndItsBoundRoute(@TempDir Path root) throws Exception {
    String owner = Type.getInternalName(HandleBindingsTests.Fixture.class);
    String signature = "(L" + owner + ";JD)I";
    var declarations = Map.of("envelope", new WitnessBindings.Declaration(owner, "envelope", signature),
        "rendezvous", new WitnessBindings.Declaration(owner, "rendezvous", "()V"),
        "wrapper", new WitnessBindings.Declaration(owner, "wrapper", signature),
        "target", new WitnessBindings.Declaration(owner, "target", "(JD)I"));
    byte[] bytes;
    try (var stream = HandleBindingsTests.Fixture.class.getResourceAsStream("/" + owner + ".class")) { bytes = stream.readAllBytes(); }
    var compiled = HandleBindings.compile(Map.of(owner, bytes), declarations);
    var values = new Properties();
    values.setProperty("schema_version", "3"); values.setProperty("profile", HandleBindings.PROFILE);
    values.setProperty("socket", root.resolve("witness.sock").toString());
    for (String name : new String[]{"build", "selection", "policy", "tooling"}) values.setProperty(name, "a".repeat(64));
    compiled.bindings().forEach((role, binding) -> {
      values.setProperty(role + ".owner", binding.declaration().owner());
      values.setProperty(role + ".name", binding.declaration().name());
      values.setProperty(role + ".descriptor", binding.declaration().descriptor());
      values.setProperty(role + ".class_sha256", binding.classSha256());
      values.setProperty(role + ".method_sha256", binding.methodSha256());
      values.setProperty(role + ".probe_id", binding.probeId());
      values.setProperty(role + ".origin", root.toUri().toString());
    });
    Map<String, String> route = Map.of("kind", "direct_method_handle", "field_owner", owner, "field_name", "HANDLE",
        "field_descriptor", "Ljava/lang/invoke/MethodHandle;", "site_instruction", "0",
        "invoke_owner", "java/lang/invoke/MethodHandle", "invoke_name", "invokeExact",
        "invoke_descriptor", signature, "reference_kind", "5", "sha256", compiled.route().sha256());
    route.forEach((key, value) -> values.setProperty("route." + key, value));
    Path path = root.resolve("witness.properties"); store(path, values);
    var plan = WitnessPlan.load(path);
    assertEquals(3, plan.version()); assertEquals(HandleBindings.PROFILE, plan.profile());
    assertEquals(compiled.route(), plan.route()); assertEquals(compiled.bindings(), plan.bindings());
    for (String key : route.keySet()) {
      var changed = new Properties(); changed.putAll(values); changed.setProperty("route." + key, "changed");
      store(path, changed); assertThrows(IllegalArgumentException.class, () -> WitnessPlan.load(path), key);
      changed.remove("route." + key); store(path, changed);
      assertThrows(IllegalArgumentException.class, () -> WitnessPlan.load(path), key);
    }
    for (String key : new String[]{"profile", "schema_version"}) {
      var changed = new Properties(); changed.putAll(values); changed.setProperty(key, key.equals("profile") ? WitnessBindings.PROFILE : "2");
      store(path, changed); assertThrows(IllegalArgumentException.class, () -> WitnessPlan.load(path));
    }
  }

  private static void store(Path path, Properties values) throws Exception {
    try (var writer = Files.newBufferedWriter(path)) { values.store(writer, null); }
  }
}
