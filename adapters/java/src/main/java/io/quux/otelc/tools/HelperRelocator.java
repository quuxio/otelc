package io.quux.otelc.tools;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Properties;
import java.util.jar.JarFile;
import java.util.jar.JarOutputStream;
import java.util.zip.ZipEntry;
import org.objectweb.asm.ClassReader;
import org.objectweb.asm.ClassWriter;
import org.objectweb.asm.Opcodes;
import org.objectweb.asm.commons.ClassRemapper;
import org.objectweb.asm.commons.Remapper;

/** Offline ASM transformation of our extension only; application and official-agent bytes are never rewritten. */
public final class HelperRelocator {
  private static final java.util.Set<String> PACKAGES = java.util.Set.of("io.quux.otelc.methods.",
      "io.quux.otelc.lifecycle.", "io.quux.otelc.witness.probe.");

  private HelperRelocator() {}

  static byte[] relocate(byte[] original, Map<String, String> packages) {
    ClassWriter output = new ClassWriter(0);
    Remapper names = new Remapper(Opcodes.ASM9) {
      @Override public String map(String internalName) { return substitute(internalName, packages, true); }
      @Override public Object mapValue(Object value) {
        if (value instanceof String text) return substitute(substitute(text, packages, true), packages, false);
        return super.mapValue(value);
      }
    };
    new ClassReader(original).accept(new ClassRemapper(output, names), 0);
    return output.toByteArray();
  }

  private static String substitute(String value, Map<String, String> packages, boolean internal) {
    if (value == null) return null;
    for (var entry : packages.entrySet()) {
      String from = internal ? entry.getKey().replace('.', '/') : entry.getKey();
      String to = internal ? entry.getValue().replace('.', '/') : entry.getValue();
      value = value.replace(from, to);
    }
    return value;
  }

  public static void main(String[] arguments) throws IOException {
    if (arguments.length != 3) throw new IllegalArgumentException("expected template, output and private mapping plan");
    Properties properties = new Properties();
    try (var reader = Files.newBufferedReader(Path.of(arguments[2]))) { properties.load(reader); }
    Map<String, String> packages = new LinkedHashMap<>();
    for (String key : properties.stringPropertyNames().stream().sorted().toList()) {
      String value = properties.getProperty(key);
      if (!PACKAGES.contains(key) || !value.matches("io\\.quux\\.otelc\\.generated\\.h[0-9a-f]{16}x\\d+\\.(methods|lifecycle|probe)\\.")) {
        throw new IllegalArgumentException("invalid private helper mapping");
      }
      packages.put(key, value);
    }
    if (packages.isEmpty()) throw new IllegalArgumentException("private helper mapping is empty");
    try (JarFile input = new JarFile(arguments[0]);
         var stream = Files.newOutputStream(Path.of(arguments[1]), StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE);
         JarOutputStream output = new JarOutputStream(stream)) {
      for (var entries = input.entries(); entries.hasMoreElements();) {
        var entry = entries.nextElement();
        String name = entry.getName();
        if (name.toUpperCase(java.util.Locale.ROOT).matches("META-INF/[^/]+\\.(SF|RSA|DSA|EC)")) {
          throw new IllegalArgumentException("signed extension relocation is unsupported");
        }
        byte[] bytes;
        try (var source = input.getInputStream(entry)) { bytes = source.readAllBytes(); }
        ZipEntry copied = new ZipEntry(substitute(name, packages, true));
        if (entry.getTime() >= 0) copied.setTime(entry.getTime());
        output.putNextEntry(copied);
        output.write(name.endsWith(".class") ? relocate(bytes, packages) : bytes);
        output.closeEntry();
      }
    }
  }
}
