package io.quux.otelc.witness;

import static org.junit.jupiter.api.Assertions.*;

import java.lang.instrument.ClassFileTransformer;
import java.lang.instrument.Instrumentation;
import java.lang.reflect.Proxy;
import java.net.URI;
import java.security.CodeSource;
import java.security.ProtectionDomain;
import java.security.cert.Certificate;
import java.util.HashMap;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.objectweb.asm.ClassWriter;

class WitnessClassCaptureTests {
  private static final String OWNER = WitnessMethodGuardTests.OWNER;
  private static final Class<?> FIXTURE = WitnessMethodGuardTests.Fixture.class;
  private static final ProtectionDomain DOMAIN = FIXTURE.getProtectionDomain();
  private static final String ORIGIN = DOMAIN.getCodeSource().getLocation().toExternalForm();

  private static final class FakeJvm {
    Class<?>[] loaded = {};
    ClassFileTransformer transformer;
    boolean removed;
    final Instrumentation instrumentation = (Instrumentation) Proxy.newProxyInstance(
        getClass().getClassLoader(), new Class<?>[] {Instrumentation.class}, (proxy, method, args) -> {
          switch (method.getName()) {
            case "getAllLoadedClasses": return loaded;
            case "addTransformer":
              assertEquals(true, args[1]);
              transformer = (ClassFileTransformer) args[0];
              return null;
            case "removeTransformer":
              assertSame(transformer, args[0]);
              removed = true;
              return true;
            default: throw new AssertionError(method.getName());
          }
        });

    void define(ClassLoader loader, String owner, Class<?> redefined, ProtectionDomain domain, byte[] bytes) {
      assertNull(((WitnessClassCapture) transformer).transform(null, loader, owner, redefined, domain, bytes));
    }
  }

  private static Map<String, WitnessBindings.Binding> bindings() throws java.io.IOException {
    Map<String, WitnessBindings.Binding> result = new HashMap<>();
    for (var binding : WitnessMethodGuardTests.bindings()) result.put(binding.role(), binding);
    return result;
  }

  private static WitnessClassCapture install(FakeJvm jvm) throws java.io.IOException {
    return WitnessClassCapture.install(jvm.instrumentation, bindings(), Map.of(OWNER, ORIGIN));
  }

  @Test void separatesIncomingBytesFromActualDefinitionAndNeverChangesTheBuffer() throws Exception {
    FakeJvm jvm = new FakeJvm();
    try (WitnessClassCapture capture = install(jvm)) {
      assertThrows(IllegalStateException.class, capture::verifyLoaded);
      jvm.define(null, null, null, null, null);
      jvm.define(null, "Unrelated", null, null, null);
      byte[] bytes = WitnessMethodGuardTests.bytes();
      byte[] original = bytes.clone();
      jvm.define(FIXTURE.getClassLoader(), OWNER, null, DOMAIN, bytes);
      assertArrayEquals(original, bytes);
      assertThrows(IllegalStateException.class, capture::verifyLoaded, "a transformer callback is not a definition");
      jvm.loaded = new Class<?>[] {String.class, FIXTURE};
      WitnessClassCapture.Loaded actual = capture.verifyLoaded().get(OWNER);
      assertSame(FIXTURE, actual.type());
      assertEquals(WitnessBindings.digest(bytes), actual.classSha256());
      assertEquals(ORIGIN, actual.origin());
      assertEquals(actual, capture.verifyLoaded().get(OWNER));
    }
    assertTrue(jvm.removed);
    assertThrows(IllegalStateException.class, ((WitnessClassCapture) jvm.transformer)::verifyLoaded);
    jvm.define(FIXTURE.getClassLoader(), OWNER, null, DOMAIN, WitnessMethodGuardTests.bytes());
  }

  @Test void rejectsInvalidRoleAndOriginSetsAndAlreadyLoadedOwners() throws Exception {
    FakeJvm jvm = new FakeJvm();
    Map<String, WitnessBindings.Binding> original = bindings();
    assertThrows(IllegalArgumentException.class, () -> WitnessClassCapture.install(jvm.instrumentation, Map.of(), Map.of()));
    Map<String, WitnessBindings.Binding> swapped = new HashMap<>(original);
    swapped.put("target", original.get("wrapper"));
    Map<String, String> expectedOrigins = Map.of(OWNER, ORIGIN);
    assertThrows(IllegalArgumentException.class, () -> WitnessClassCapture.install(jvm.instrumentation, swapped, expectedOrigins));
    for (Map<String, String> origins : java.util.List.of(Map.<String, String>of(), Map.of(OWNER, ""), Map.of("Other", ORIGIN))) {
      assertThrows(IllegalArgumentException.class, () -> WitnessClassCapture.install(jvm.instrumentation, original, origins));
    }
    jvm.loaded = new Class<?>[] {FIXTURE};
    assertThrows(IllegalStateException.class, () -> install(jvm));
    assertNull(jvm.transformer);
  }

  @Test void inputFailuresRemainStickyEvenIfLaterBytesAreCorrect() throws Exception {
    byte[] correct = WitnessMethodGuardTests.bytes();
    for (String mutation : java.util.List.of("loader", "origin", "null-domain", "null-code-source", "null-location", "bytes", "redefine", "duplicate", "invalid-class", "owner", "method")) {
      FakeJvm jvm = new FakeJvm();
      Map<String, WitnessBindings.Binding> expected = bindings();
      byte[] incoming = correct.clone();
      ClassLoader loader = FIXTURE.getClassLoader();
      ProtectionDomain domain = DOMAIN;
      Class<?> redefined = null;
      switch (mutation) {
        case "loader" -> loader = null;
        case "origin" -> domain = new ProtectionDomain(new CodeSource(URI.create("file:/changed/").toURL(), (Certificate[]) null), null);
        case "null-domain" -> domain = null;
        case "null-code-source" -> domain = new ProtectionDomain(null, null);
        case "null-location" -> domain = new ProtectionDomain(new CodeSource(null, (Certificate[]) null), null);
        case "bytes" -> incoming[10] ^= 1;
        case "redefine" -> redefined = FIXTURE;
        case "invalid-class" -> {
          incoming = new byte[] {0, 1, 2};
          expected = rebind(expected, WitnessBindings.digest(incoming), null);
        }
        case "owner" -> {
          var type = WitnessBindings.read(correct);
          type.name = "Changed";
          ClassWriter writer = new ClassWriter(0);
          type.accept(writer);
          incoming = writer.toByteArray();
          expected = rebind(expected, WitnessBindings.digest(incoming), null);
        }
        case "method" -> expected = rebind(expected, null, "0".repeat(64));
        case "duplicate" -> { /* The second callback below supplies the duplicate definition. */ }
        default -> throw new AssertionError(mutation);
      }
      try (var capture = WitnessClassCapture.install(jvm.instrumentation, expected, Map.of(OWNER, ORIGIN))) {
        jvm.define(loader, OWNER, redefined, domain, incoming);
        jvm.define(FIXTURE.getClassLoader(), OWNER, null, DOMAIN, correct);
        jvm.loaded = new Class<?>[] {FIXTURE};
        assertThrows(IllegalStateException.class, capture::verifyLoaded, mutation);
      }
    }
  }

  private static Map<String, WitnessBindings.Binding> rebind(Map<String, WitnessBindings.Binding> original,
                                                            String classHash, String methodHash) {
    Map<String, WitnessBindings.Binding> result = new HashMap<>();
    original.forEach((role, binding) -> result.put(role, new WitnessBindings.Binding(role, binding.declaration(),
        classHash == null ? binding.classSha256() : classHash,
        methodHash == null ? binding.methodSha256() : methodHash, binding.probeId())));
    return result;
  }

  @Test void rejectsUncapturedAndAmbiguousLoadedIdentities() throws Exception {
    for (String mutation : java.util.List.of("missing", "duplicate", "foreign-loader")) {
      FakeJvm jvm = new FakeJvm();
      try (var capture = install(jvm)) {
        if (!mutation.equals("missing")) {
          jvm.define(FIXTURE.getClassLoader(), OWNER, null, DOMAIN, WitnessMethodGuardTests.bytes());
        }
        if (mutation.equals("foreign-loader")) {
          var loader = new net.bytebuddy.dynamic.loading.ByteArrayClassLoader.ChildFirst(
              FIXTURE.getClassLoader(), Map.of(FIXTURE.getName(), WitnessMethodGuardTests.bytes()));
          jvm.loaded = new Class<?>[] {loader.loadClass(FIXTURE.getName())};
        } else {
          jvm.loaded = mutation.equals("duplicate") ? new Class<?>[] {FIXTURE, FIXTURE} : new Class<?>[] {FIXTURE};
        }
        assertThrows(IllegalStateException.class, capture::verifyLoaded, mutation);
      }
    }
  }
}
