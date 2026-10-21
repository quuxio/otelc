package io.quux.otelc.tools;

import static org.junit.jupiter.api.Assertions.*;

import io.opentelemetry.api.trace.Span;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import java.util.jar.JarOutputStream;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.objectweb.asm.ClassReader;
import org.objectweb.asm.tree.ClassNode;
import org.objectweb.asm.tree.LdcInsnNode;

class HelperRelocatorTests {
  @TempDir Path root;
  private static final String PREFIX = "io.quux.otelc.generated.h0123456789abcdefx0.";

  private static byte[] resource(String path) throws Exception {
    try (var input = HelperRelocatorTests.class.getResourceAsStream("/" + path)) { return input.readAllBytes(); }
  }

  @Test void remapsExecutableTypesAndReflectiveNamesWithTheExistingAsmLibrary() throws Exception {
    byte[] original = resource("io/quux/otelc/methods/SafeSpan.class");
    Map<String, String> packages = Map.of("io.quux.otelc.methods.", PREFIX + "methods.",
        "io.quux.otelc.lifecycle.", PREFIX + "lifecycle.");
    byte[] relocated = HelperRelocator.relocate(original, packages);
    assertEquals((PREFIX + "methods.SafeSpan").replace('.', '/'), new ClassReader(relocated).getClassName());
    class Loader extends ClassLoader {
      Loader() { super(HelperRelocatorTests.class.getClassLoader()); }
      Class<?> define(byte[] bytes) { return defineClass(null, bytes, 0, bytes.length); }
    }
    Object actual = new Loader().define(relocated).getConstructor(Span.class).newInstance(Span.getInvalid());
    assertEquals(Span.getInvalid().getSpanContext(), ((Span) actual).getSpanContext());
    ClassNode state = new ClassNode();
    new ClassReader(HelperRelocator.relocate(resource("io/quux/otelc/LiveState.class"), packages)).accept(state, 0);
    assertTrue(state.methods.stream().flatMap(method -> java.util.Arrays.stream(method.instructions.toArray()))
        .filter(LdcInsnNode.class::isInstance).map(LdcInsnNode.class::cast)
        .anyMatch(instruction -> (PREFIX + "lifecycle.Observer").equals(instruction.cst)));
  }

  @Test void generatesAnIndependentJarAndRefusesInvalidMappingsAndSignedInputs() throws Exception {
    Path input = root.resolve("template.jar");
    byte[] helper = resource("io/quux/otelc/methods/SafeSpan.class");
    try (var jar = new JarOutputStream(Files.newOutputStream(input))) {
      for (var entry : Map.of("io/quux/otelc/methods/SafeSpan.class", helper, "config.txt", new byte[] {1, 2, 3}).entrySet()) {
        jar.putNextEntry(new ZipEntry(entry.getKey())); jar.write(entry.getValue()); jar.closeEntry();
      }
    }
    byte[] unchanged = Files.readAllBytes(input);
    Path plan = root.resolve("map.properties");
    Files.writeString(plan, "io.quux.otelc.methods.=" + PREFIX + "methods.\n");
    Path output = root.resolve("generated.jar");
    HelperRelocator.main(new String[] {input.toString(), output.toString(), plan.toString()});
    assertArrayEquals(unchanged, Files.readAllBytes(input));
    try (var jar = new ZipFile(output.toFile())) {
      assertNull(jar.getEntry("io/quux/otelc/methods/SafeSpan.class"));
      assertNotNull(jar.getEntry((PREFIX + "methods.SafeSpan").replace('.', '/') + ".class"));
      assertArrayEquals(new byte[] {1, 2, 3}, jar.getInputStream(jar.getEntry("config.txt")).readAllBytes());
    }
    assertThrows(IllegalArgumentException.class, () -> HelperRelocator.main(new String[0]));
    Path invalidOutput = root.resolve("invalid.jar");
    for (String invalid : new String[] {"", "other=x\n", "io.quux.otelc.methods.=untrusted.\n"}) {
      Files.writeString(plan, invalid);
      String[] arguments = {input.toString(), invalidOutput.toString(), plan.toString()};
      assertThrows(IllegalArgumentException.class, () -> HelperRelocator.main(arguments));
    }
    Files.writeString(plan, "io.quux.otelc.methods.=" + PREFIX + "methods.\n");
    Path signed = root.resolve("signed.jar");
    try (var jar = new JarOutputStream(Files.newOutputStream(signed))) {
      jar.putNextEntry(new ZipEntry("META-INF/SIGNATURE.SF")); jar.write(1); jar.closeEntry();
    }
    String[] arguments = {signed.toString(), invalidOutput.toString(), plan.toString()};
    assertThrows(IllegalArgumentException.class, () -> HelperRelocator.main(arguments));
  }
}
