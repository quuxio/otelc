package io.quux.otelc.witness;

import java.lang.instrument.ClassFileTransformer;
import java.lang.instrument.Instrumentation;
import java.security.ProtectionDomain;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Read-only input evidence; a match never proves probe execution or final transformed code. */
public final class WitnessClassCapture implements ClassFileTransformer, AutoCloseable {
  private static final Set<String> ROLES = Set.of("envelope", "rendezvous", "wrapper", "target");
  private final Instrumentation instrumentation;
  private final Map<String, List<WitnessBindings.Binding>> expected;
  private final Map<String, String> origins;
  private final HandleBindings.Route route;
  private final Map<String, Input> captured = new HashMap<>();
  private final Map<String, byte[]> originals = new HashMap<>();
  private boolean compiled;
  private String failure;
  private boolean closed;

  /** Actual loaded identity paired with the original bytes observed at this transformer. */
  public record Loaded(Class<?> type, String classSha256, String origin) {}

  private record Input(String classSha256, String origin) {}

  private WitnessClassCapture(Instrumentation instrumentation,
                              Map<String, WitnessBindings.Binding> bindings,
                              Map<String, String> origins, HandleBindings.Route route) {
    if (!bindings.keySet().equals(ROLES)) throw new IllegalArgumentException("four witness roles are required");
    Map<String, List<WitnessBindings.Binding>> byOwner = new HashMap<>();
    for (var entry : bindings.entrySet()) {
      WitnessBindings.Binding binding = entry.getValue();
      if (!entry.getKey().equals(binding.role())) throw new IllegalArgumentException("witness role disagrees with binding");
      byOwner.computeIfAbsent(binding.declaration().owner(), ignored -> new java.util.ArrayList<>()).add(binding);
    }
    if (!byOwner.keySet().equals(origins.keySet()) || origins.values().stream().anyMatch(value -> value == null || value.isEmpty())) {
      throw new IllegalArgumentException("an exact code-source origin is required for every witness owner");
    }
    this.instrumentation = instrumentation;
    this.expected = Map.copyOf(byOwner);
    this.origins = Map.copyOf(origins);
    this.route = route;
  }

  /** Install before the pinned agent's main transformer, without loading application owners. */
  public static WitnessClassCapture install(Instrumentation instrumentation,
                                             Map<String, WitnessBindings.Binding> bindings,
                                             Map<String, String> origins) {
    return install(instrumentation, bindings, origins, null);
  }

  public static WitnessClassCapture install(Instrumentation instrumentation,
                                             Map<String, WitnessBindings.Binding> bindings,
                                             Map<String, String> origins, HandleBindings.Route route) {
    WitnessClassCapture capture = new WitnessClassCapture(instrumentation, bindings, origins, route);
    // Warm up hashing before using it under JVM loader locks.
    WitnessBindings.digest(new byte[0]);
    for (Class<?> type : instrumentation.getAllLoadedClasses()) {
      if (capture.expected.containsKey(type.getName().replace('.', '/'))) {
        throw new IllegalStateException("witness owner was loaded before input capture");
      }
    }
    instrumentation.addTransformer(capture, true);
    return capture;
  }

  @Override public byte[] transform(Module module, ClassLoader loader, String name,
                                   Class<?> redefined, ProtectionDomain domain, byte[] bytes) {
    if (name == null || !expected.containsKey(name)) return null;
    synchronized (this) {
      if (closed || failure != null) return null;
      try {
        capture(loader, name, redefined, domain, bytes);
      } catch (RuntimeException | LinkageError invalid) {
        // The JVM ignores transformer exceptions. Keep failure sticky instead of throwing it away.
        fail("witness input validation failed");
      }
    }
    return null;
  }

  private void capture(ClassLoader loader, String name, Class<?> redefined,
                       ProtectionDomain domain, byte[] bytes) {
    if (bytes.length > 16 * 1024 * 1024) {
      fail("witness class exceeds capture capacity");
      return;
    }
    if (loader != ClassLoader.getSystemClassLoader()) {
      fail("witness owner used an unsupported classloader");
      return;
    }
    if (redefined != null || captured.containsKey(name)) {
      fail("witness owner was redefined or loaded more than once");
      return;
    }
    String origin = origin(domain);
    String digest = WitnessBindings.digest(bytes);
    if (!origin.equals(origins.get(name))) {
      fail("witness code-source origin changed");
      return;
    }
    if (expected.get(name).stream().anyMatch(binding -> !digest.equals(binding.classSha256()))) {
      fail("witness input bytes changed");
      return;
    }
    var type = WitnessBindings.read(bytes);
    if (!name.equals(type.name)) {
      fail("witness input owner changed");
      return;
    }
    for (WitnessBindings.Binding binding : expected.get(name)) {
      if (!WitnessBindings.methodDigest(binding.declaration(),
          WitnessBindings.find(type, binding.declaration())).equals(binding.methodSha256())) {
        fail("witness input bytes changed");
        return;
      }
    }
    captured.put(name, new Input(digest, origin));
    originals.put(name, bytes.clone());
  }

  private void fail(String reason) {
    if (failure == null) failure = reason;
    originals.clear();
  }

  /** Recompile the complete four-role shape from actual captured bytes, then discard them. */
  public void verifyBindings() {
    verifyLoaded();
    synchronized (this) {
      if (closed || failure != null) throw new IllegalStateException("witness capture is unavailable");
      if (compiled) return;
      Map<String, WitnessBindings.Binding> bindings = new HashMap<>();
      Map<String, WitnessBindings.Declaration> declarations = new HashMap<>();
      expected.values().forEach(group -> group.forEach(binding -> {
        bindings.put(binding.role(), binding);
        declarations.put(binding.role(), binding.declaration());
      }));
      try {
        Map<String, WitnessBindings.Binding> compiledBindings;
        if (route == null) compiledBindings = WitnessBindings.compile(originals, declarations);
        else {
          var compiled = HandleBindings.compile(originals, declarations);
          if (!route.equals(compiled.route())) throw new IllegalStateException("captured handle route changed");
          compiledBindings = compiled.bindings();
        }
        if (!compiledBindings.equals(bindings)) {
          throw new IllegalStateException("compiled witness bindings disagree with captured code");
        }
        compiled = true;
      } catch (RuntimeException invalid) {
        fail("captured witness structure is invalid");
        throw new IllegalStateException(failure, invalid);
      } finally {
        originals.clear();
      }
    }
  }

  private static String origin(ProtectionDomain domain) {
    if (domain == null || domain.getCodeSource() == null || domain.getCodeSource().getLocation() == null) return "";
    return domain.getCodeSource().getLocation().toExternalForm();
  }

  /** Refuse missing, invalid, retired or ambiguous evidence. Does not initialise any owner. */
  public Map<String, Loaded> verifyLoaded() {
    Class<?>[] loaded = instrumentation.getAllLoadedClasses();
    synchronized (this) {
      if (closed) throw new IllegalStateException("witness input capture is closed");
      if (failure != null) throw new IllegalStateException(failure);
      Map<String, Loaded> result = new HashMap<>();
      for (Class<?> type : loaded) {
        verifyType(type, result);
      }
      if (!result.keySet().equals(expected.keySet())) throw new IllegalStateException("witness owner is not yet defined");
      return Map.copyOf(result);
    }
  }

  private void verifyType(Class<?> type, Map<String, Loaded> result) {
    String name = type.getName().replace('.', '/');
    if (!expected.containsKey(name)) return;
    Input input = captured.get(name);
    if (type.getClassLoader() != ClassLoader.getSystemClassLoader() || input == null
        || !input.origin().equals(origin(type.getProtectionDomain())) || result.containsKey(name)) {
      fail("loaded witness identity disagrees with captured input");
      throw new IllegalStateException(failure);
    }
    result.put(name, new Loaded(type, input.classSha256(), input.origin()));
  }

  @Override public void close() {
    synchronized (this) { closed = true; originals.clear(); }
    instrumentation.removeTransformer(this);
  }
}
