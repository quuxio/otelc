package io.quux.otelc.witness;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.Set;

/** Private generated plan, separate from the common user policy and closed launch/health APIs. */
public record WitnessPlan(Path socket, Map<String, WitnessBindings.Binding> bindings,
                          Map<String, String> origins, String build, String selection,
                          String policy, String tooling) {
  public static final List<String> ROLES = List.of("envelope", "rendezvous", "wrapper", "target");
  private static final Set<String> FIELDS = Set.of("owner", "name", "descriptor", "class_sha256",
      "method_sha256", "probe_id", "origin");

  public WitnessPlan {
    bindings = Map.copyOf(bindings);
    origins = Map.copyOf(origins);
  }

  public static WitnessPlan load(Path file) throws IOException {
    Properties values = new Properties() {
      @Override public synchronized Object put(Object key, Object value) {
        if (containsKey(key)) throw new IllegalArgumentException("duplicate witness plan key");
        return super.put(key, value);
      }
    };
    if (Files.size(file) > 32768) throw new IllegalArgumentException("witness plan exceeds its bound");
    try (var reader = Files.newBufferedReader(file)) { values.load(reader); }
    validateShape(values);
    Map<String, WitnessBindings.Binding> bindings = new HashMap<>();
    Map<String, String> origins = new HashMap<>();
    for (String role : ROLES) {
      String prefix = role + ".";
      var declaration = new WitnessBindings.Declaration(values.getProperty(prefix + "owner"),
          values.getProperty(prefix + "name"), values.getProperty(prefix + "descriptor"));
      String classHash = hash(values.getProperty(prefix + "class_sha256"));
      String methodHash = hash(values.getProperty(prefix + "method_sha256"));
      String probe = hash(values.getProperty(prefix + "probe_id"));
      String expected = WitnessBindings.digest(String.join("\n", WitnessBindings.PROFILE, role,
          declaration.owner(), declaration.name(), declaration.descriptor(), classHash, methodHash)
          .getBytes(java.nio.charset.StandardCharsets.UTF_8));
      if (!probe.equals(expected)) throw new IllegalArgumentException("witness probe ID disagrees with binding");
      bindings.put(role, new WitnessBindings.Binding(role, declaration, classHash, methodHash, probe));
      String origin = values.getProperty(prefix + "origin");
      if (!origin.startsWith("file:/")) throw new IllegalArgumentException("witness requires explicit file origins");
      String previous = origins.putIfAbsent(declaration.owner(), origin);
      if (previous != null && !previous.equals(origin)) throw new IllegalArgumentException("conflicting witness origins");
    }
    if (bindings.values().stream().map(WitnessBindings.Binding::declaration).distinct().count() != 4) {
      throw new IllegalArgumentException("witness roles must be distinct");
    }
    return new WitnessPlan(Path.of(values.getProperty("socket")), bindings, origins,
        hash(values.getProperty("build")), hash(values.getProperty("selection")),
        hash(values.getProperty("policy")), hash(values.getProperty("tooling")));
  }

  private static void validateShape(Properties values) {
    Set<String> allowed = new HashSet<>(Set.of("schema_version", "profile", "socket", "build",
        "selection", "policy", "tooling"));
    for (String role : ROLES) for (String field : FIELDS) allowed.add(role + "." + field);
    if (!values.stringPropertyNames().equals(allowed) || !"1".equals(values.getProperty("schema_version"))
        || !WitnessBindings.PROFILE.equals(values.getProperty("profile"))) {
      throw new IllegalArgumentException("unsupported or incomplete witness plan");
    }
  }

  private static String hash(String value) {
    if (value == null || !value.matches("[0-9a-f]{64}")) throw new IllegalArgumentException("invalid witness digest");
    return value;
  }
}
