package io.quux.otelc.witness;

import java.io.IOException;
import java.io.ByteArrayInputStream;
import java.io.InputStreamReader;
import java.nio.ByteBuffer;
import java.nio.channels.SeekableByteChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.BasicFileAttributes;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.Objects;
import java.util.Set;

/** Private generated plan, separate from the common user policy and closed launch/health APIs. */
public record WitnessPlan(Path socket, Map<String, WitnessBindings.Binding> bindings,
                          Map<String, String> origins, String build, String selection,
                          String policy, String tooling, HandleBindings.Route route) {
  public static final List<String> ROLES = List.of("envelope", "rendezvous", "wrapper", "target");
  private static final Set<String> FIELDS = Set.of("owner", "name", "descriptor", "class_sha256",
      "method_sha256", "probe_id", "origin");
  private static final int MAX_BYTES = 32768;
  private static final Set<String> ROUTE_FIELDS = Set.of("kind", "field_owner", "field_name", "field_descriptor",
      "site_instruction", "invoke_owner", "invoke_name", "invoke_descriptor", "reference_kind", "sha256");

  public WitnessPlan(Path socket, Map<String, WitnessBindings.Binding> bindings,
                      Map<String, String> origins, String build, String selection, String policy, String tooling) {
    this(socket, bindings, origins, build, selection, policy, tooling, null);
  }

  public String profile() { return route == null ? WitnessBindings.PROFILE : HandleBindings.PROFILE; }
  public int version() { return route == null ? 2 : 3; }

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
    byte[] content = planBytes(file);
    try (var reader = new InputStreamReader(new ByteArrayInputStream(content), StandardCharsets.UTF_8.newDecoder())) {
      values.load(reader);
    }
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
      String expected = WitnessBindings.digest(String.join("\n", values.getProperty("profile"), role,
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
    HandleBindings.Route route = HandleBindings.PROFILE.equals(values.getProperty("profile"))
        ? route(values, bindings) : null;
    return new WitnessPlan(Path.of(values.getProperty("socket")), bindings, origins,
        hash(values.getProperty("build")), hash(values.getProperty("selection")),
        hash(values.getProperty("policy")), hash(values.getProperty("tooling")), route);
  }

  private static HandleBindings.Route route(Properties values, Map<String, WitnessBindings.Binding> bindings) {
    if (!"direct_method_handle".equals(values.getProperty("route.kind"))
        || !"Ljava/lang/invoke/MethodHandle;".equals(values.getProperty("route.field_descriptor"))
        || !"0".equals(values.getProperty("route.site_instruction"))
        || !"java/lang/invoke/MethodHandle".equals(values.getProperty("route.invoke_owner"))
        || !"invokeExact".equals(values.getProperty("route.invoke_name"))
        || !Set.of("5", "6").contains(values.getProperty("route.reference_kind"))) {
      throw new IllegalArgumentException("unsupported witness handle route");
    }
    String owner = values.getProperty("route.field_owner"), name = values.getProperty("route.field_name");
    String descriptor = values.getProperty("route.invoke_descriptor");
    int kind = Integer.parseInt(values.getProperty("route.reference_kind"));
    String digest = hash(values.getProperty("route.sha256"));
    if (!owner.equals(bindings.get("wrapper").declaration().owner())
        || !descriptor.equals(bindings.get("wrapper").declaration().descriptor())
        || !digest.equals(HandleBindings.routeDigest(owner, name, descriptor, kind, bindings))) {
      throw new IllegalArgumentException("witness handle route digest disagrees with binding");
    }
    return new HandleBindings.Route(owner, name, descriptor, kind, digest);
  }

  private static byte[] planBytes(Path file) throws IOException {
    // The producer consumes a generated file in its owned private runtime,
    // before application code starts. Arbitrary concurrent writers are outside
    // this transport; observed replacement or growth must still fail closed.
    BasicFileAttributes before = Files.readAttributes(file, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
    if (!before.isRegularFile() || before.size() > MAX_BYTES) {
      throw new IllegalArgumentException("witness plan must be a bounded regular file");
    }
    try (var input = Files.newByteChannel(file, StandardOpenOption.READ, LinkOption.NOFOLLOW_LINKS)) {
      byte[] content = boundedBytes(input, before.size());
      BasicFileAttributes after = Files.readAttributes(file, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
      if (!after.isRegularFile() || before.fileKey() == null || !Objects.equals(before.fileKey(), after.fileKey())
          || before.size() != after.size() || !before.lastModifiedTime().equals(after.lastModifiedTime())
          || input.size() != before.size()) {
        throw new IOException("witness plan changed while reading");
      }
      return content;
    }
  }

  static byte[] boundedBytes(SeekableByteChannel input, long expected) throws IOException {
    ByteBuffer bytes = ByteBuffer.allocate(MAX_BYTES + 1);
    while (bytes.hasRemaining()) {
      int count = input.read(bytes);
      if (count < 0) break;
      if (count == 0) throw new IOException("witness plan read made no progress");
    }
    if (bytes.position() > MAX_BYTES) throw new IllegalArgumentException("witness plan exceeds its bound");
    if (bytes.position() != expected || input.size() != expected) {
      throw new IOException("witness plan changed while reading");
    }
    return Arrays.copyOf(bytes.array(), bytes.position());
  }

  private static void validateShape(Properties values) {
    Set<String> allowed = new HashSet<>(Set.of("schema_version", "profile", "socket", "build",
        "selection", "policy", "tooling"));
    for (String role : ROLES) for (String field : FIELDS) allowed.add(role + "." + field);
    boolean handle = HandleBindings.PROFILE.equals(values.getProperty("profile"));
    if (handle) for (String field : ROUTE_FIELDS) allowed.add("route." + field);
    if (!values.stringPropertyNames().equals(allowed)
        || !(handle ? "3" : "2").equals(values.getProperty("schema_version"))
        || !handle && !WitnessBindings.PROFILE.equals(values.getProperty("profile"))) {
      throw new IllegalArgumentException("unsupported or incomplete witness plan");
    }
  }

  private static String hash(String value) {
    if (value == null || !value.matches("[0-9a-f]{64}")) throw new IllegalArgumentException("invalid witness digest");
    return value;
  }
}
