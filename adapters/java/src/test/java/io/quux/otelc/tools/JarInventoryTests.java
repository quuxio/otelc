package io.quux.otelc.tools;

import static org.junit.jupiter.api.Assertions.*;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.jar.Attributes;
import java.util.jar.JarOutputStream;
import java.util.jar.Manifest;
import java.util.zip.ZipEntry;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class JarInventoryTests {
  @TempDir Path root;

  private Path jar(String name, Manifest manifest, Map<String, byte[]> entries) throws IOException {
    Path path = root.resolve(name);
    try (var output = new JarOutputStream(Files.newOutputStream(path), manifest)) {
      for (var entry : entries.entrySet()) {
        output.putNextEntry(new ZipEntry(entry.getKey()));
        output.write(entry.getValue()); output.closeEntry();
      }
    }
    return path;
  }

  private static Manifest manifest(boolean multiRelease) {
    Manifest manifest = new Manifest();
    manifest.getMainAttributes().put(Attributes.Name.MANIFEST_VERSION, "1.0");
    if (multiRelease) manifest.getMainAttributes().putValue("Multi-Release", "true");
    return manifest;
  }

  private static String digest(byte[] bytes) throws Exception {
    return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes));
  }

  @Test void delegatesPerClassRuntimeSelectionAndRetainsActualMembersAndPackageInfo() throws Exception {
    int feature = Runtime.version().feature();
    Map<String, byte[]> entries = new LinkedHashMap<>();
    entries.put("Thing.class", new byte[] {8});
    entries.put("Other.class", new byte[] {8});
    entries.put("p/package-info.class", new byte[] {1});
    entries.put("module-info.class", new byte[] {2});
    entries.put("resource.txt", new byte[] {3});
    entries.put("META-INF/versions/9/Thing.class", new byte[] {9});
    entries.put("META-INF/versions/9/Other.class", new byte[] {9});
    entries.put("META-INF/versions/" + feature + "/Thing.class", new byte[] {21});
    entries.put("META-INF/versions/" + feature + "/Only.class", new byte[] {1});
    entries.put("META-INF/versions/" + (feature + 1) + "/Thing.class", new byte[] {99});
    for (boolean active : new boolean[] {false, true}) {
      Path path = jar(active + ".jar", manifest(active), entries);
      byte[] original = Files.readAllBytes(path);
      var result = JarInventory.inspect(path);
      assertEquals(path.toRealPath().toString(), result.path());
      assertEquals(digest(original), result.sha256());
      assertEquals(active, result.multiRelease());
      assertTrue(result.versionedMembers());
      assertEquals(active ? "META-INF/versions/" + feature + "/Thing.class" : "Thing.class",
          result.classes().get("Thing").entry());
      assertEquals(digest(new byte[] {(byte) (active ? 21 : 8)}), result.classes().get("Thing").sha256());
      assertEquals(active ? "META-INF/versions/9/Other.class" : "Other.class", result.classes().get("Other").entry());
      assertTrue(result.classes().containsKey("p.package-info"));
      assertFalse(result.classes().containsKey("module-info"));
      assertEquals(active, result.classes().containsKey("Only"));
      assertEquals(active ? 4 : 3, result.classes().size());
      assertArrayEquals(original, Files.readAllBytes(path));
    }
  }

  @Test void identicalBytesDoNotEraseTheEffectiveMemberIdentity() throws Exception {
    byte[] bytes = {1, 2, 3};
    String versioned = "META-INF/versions/" + Runtime.version().feature() + "/Same.class";
    var result = JarInventory.inspect(jar("same.jar", manifest(true), Map.of("Same.class", bytes, versioned, bytes)));
    assertEquals(versioned, result.classes().get("Same").entry());
    assertEquals(digest(bytes), result.classes().get("Same").sha256());
  }

  @Test void foldedManifestUsesTheActualRuntimeDecisionRatherThanTheParsedAttribute() throws Exception {
    Path path = root.resolve("folded.jar");
    try (var output = new JarOutputStream(Files.newOutputStream(path))) {
      output.putNextEntry(new ZipEntry("META-INF/MANIFEST.MF"));
      output.write("Manifest-Version: 1.0\r\nMulti-Release: tr\r\n ue\r\n\r\n".getBytes(StandardCharsets.UTF_8));
      output.closeEntry();
      for (String name : List.of("Flags.class", "META-INF/versions/21/Flags.class")) {
        output.putNextEntry(new ZipEntry(name)); output.write(name.getBytes(StandardCharsets.UTF_8)); output.closeEntry();
      }
    }
    var result = JarInventory.inspect(path);
    try (var jar = new java.util.jar.JarFile(path.toFile(), true, java.util.zip.ZipFile.OPEN_READ, Runtime.version())) {
      assertEquals("true", jar.getManifest().getMainAttributes().getValue("Multi-Release"));
      assertEquals(jar.isMultiRelease(), result.multiRelease());
      assertEquals(jar.getJarEntry("Flags.class").getRealName(), result.classes().get("Flags").entry());
    }
  }

  @Test void refusesImplicitResolutionDuplicateMembersAndPropertyOverrides() throws Exception {
    Manifest implicit = manifest(false);
    implicit.getMainAttributes().put(Attributes.Name.CLASS_PATH, "other.jar");
    Path path = jar("implicit.jar", implicit, Map.of());
    assertThrows(IllegalArgumentException.class, () -> JarInventory.inspect(path));
    Path indexed = jar("indexed.jar", manifest(false), Map.of("META-INF/INDEX.LIST", new byte[] {1}));
    assertThrows(IllegalArgumentException.class, () -> JarInventory.inspect(indexed));
    Path duplicate = jar("duplicate.jar", manifest(false), Map.of("X.class", new byte[] {1}, "Y.class", new byte[] {2}));
    // Replace equal-length names in local/CEN records; JarOutputStream itself prevents duplicate names.
    Files.write(duplicate, new String(Files.readAllBytes(duplicate), StandardCharsets.ISO_8859_1)
        .replace("Y.class", "X.class").getBytes(StandardCharsets.ISO_8859_1));
    assertThrows(IllegalArgumentException.class, () -> JarInventory.inspect(duplicate));
    for (String property : List.of("jdk.util.jar.version", "jdk.util.jar.enableMultiRelease")) {
      String previous = System.getProperty(property);
      try {
        System.setProperty(property, "");
        assertThrows(IllegalArgumentException.class, JarInventory::requireDefaultSelection);
      } finally {
        if (previous == null) System.clearProperty(property); else System.setProperty(property, previous);
      }
    }
    Path invalid = root.resolve("invalid.jar"); Files.writeString(invalid, "not a JAR");
    assertThrows(IOException.class, () -> JarInventory.inspect(invalid));
  }

  @Test void emitsAStandaloneProtocolAndEscapesUntrustedNamesWithoutRunningApplicationCode() throws Exception {
    assertEquals("\"a\\u000a\\\"\\\\\"", JarInventory.quote("a\n\"\\"));
    Path path = jar("ordinary.jar", manifest(false), Map.of("Plain.class", new byte[] {7}));
    var result = JarInventory.inspect(path);
    assertFalse(result.versionedMembers());
    assertThrows(IllegalArgumentException.class, () -> JarInventory.main(new String[0]));
    String[] duplicate = {path.toString(), path.toString()};
    assertThrows(IllegalArgumentException.class, () -> JarInventory.main(duplicate));
    PrintStream previous = System.out;
    ByteArrayOutputStream output = new ByteArrayOutputStream();
    try (var stream = new PrintStream(output, true, StandardCharsets.UTF_8)) {
      System.setOut(stream);
      JarInventory.main(new String[] {path.toString()});
    } finally { System.setOut(previous); }
    assertEquals(JarInventory.json(List.of(result)) + System.lineSeparator(), output.toString(StandardCharsets.UTF_8));
    assertTrue(output.toString(StandardCharsets.UTF_8).contains("\"jdk_feature\":" + Runtime.version().feature()));
  }
}
