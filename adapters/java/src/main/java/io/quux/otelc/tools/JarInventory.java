package io.quux.otelc.tools;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.DigestInputStream;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.jar.Attributes;
import java.util.jar.JarFile;
import java.util.zip.ZipFile;

/** Offline selected-JDK archive resolution. No application class is loaded or executed. */
public final class JarInventory {
  private JarInventory() {}

  public record Member(String entry, String sha256) {}
  public record Archive(String path, String sha256, boolean multiRelease,
      boolean versionedMembers, Map<String, Member> classes) {}

  static void requireDefaultSelection() {
    if (System.getProperty("jdk.util.jar.version") != null
        || System.getProperty("jdk.util.jar.enableMultiRelease") != null) {
      throw new IllegalArgumentException("runtime archive profile requires default JDK multi-release properties");
    }
  }

  static String sha256(InputStream source) throws IOException, NoSuchAlgorithmException {
    MessageDigest digest = MessageDigest.getInstance("SHA-256");
    try (var input = new DigestInputStream(source, digest)) { input.transferTo(OutputStream.nullOutputStream()); }
    return HexFormat.of().formatHex(digest.digest());
  }

  public static Archive inspect(Path path) throws IOException, NoSuchAlgorithmException {
    requireDefaultSelection();
    Path canonical = path.toRealPath();
    String original = sha256(Files.newInputStream(canonical));
    Map<String, Member> classes = new TreeMap<>();
    boolean versioned = false;
    boolean multiRelease;
    try (var jar = new JarFile(canonical.toFile(), true, ZipFile.OPEN_READ, Runtime.version())) {
      var names = new HashSet<String>();
      for (var entries = jar.entries(); entries.hasMoreElements();) {
        String name = entries.nextElement().getName();
        if (!names.add(name)) throw new IllegalArgumentException("duplicate JAR member: " + name);
        versioned |= name.startsWith("META-INF/versions/");
      }
      if (names.contains("META-INF/INDEX.LIST")) throw new IllegalArgumentException("implicit JAR indexes are unsupported");
      var manifest = jar.getManifest();
      if (manifest != null && manifest.getMainAttributes().getValue(Attributes.Name.CLASS_PATH) != null) {
        throw new IllegalArgumentException("implicit JAR Class-Path is unsupported; supply the complete classpath");
      }
      multiRelease = jar.isMultiRelease();
      try (var stream = jar.versionedStream()) {
        for (var entries = stream.iterator(); entries.hasNext();) {
          var entry = entries.next();
          String name = entry.getName();
          if (entry.isDirectory() || !name.endsWith(".class") || name.startsWith("META-INF/")
              || name.equals("module-info.class")) continue;
          String binaryName = name.substring(0, name.length() - 6).replace('/', '.');
          classes.put(binaryName, new Member(entry.getRealName(), sha256(jar.getInputStream(entry))));
        }
      }
    }
    if (!original.equals(sha256(Files.newInputStream(canonical)))) {
      throw new IllegalArgumentException("JAR changed during runtime archive resolution");
    }
    return new Archive(canonical.toString(), original, multiRelease, versioned, Collections.unmodifiableMap(classes));
  }

  static String quote(String text) {
    StringBuilder result = new StringBuilder("\"");
    for (char value : text.toCharArray()) {
      if (value == '\\' || value == '"') result.append('\\').append(value);
      else if (value < 32) result.append(String.format(java.util.Locale.ROOT, "\\u%04x", (int) value));
      else result.append(value);
    }
    return result.append('"').toString();
  }

  static String json(List<Archive> archives) {
    StringBuilder result = new StringBuilder("{\"schema_version\":1,\"jdk_feature\":")
        .append(Runtime.version().feature()).append(",\"archives\":[");
    String separator = "";
    for (Archive archive : archives) {
      result.append(separator).append("{\"path\":").append(quote(archive.path()))
          .append(",\"sha256\":").append(quote(archive.sha256()))
          .append(",\"multi_release\":").append(archive.multiRelease())
          .append(",\"versioned_members\":").append(archive.versionedMembers()).append(",\"classes\":{");
      String memberSeparator = "";
      for (var member : archive.classes().entrySet()) {
        result.append(memberSeparator).append(quote(member.getKey())).append(":{\"entry\":")
            .append(quote(member.getValue().entry())).append(",\"sha256\":")
            .append(quote(member.getValue().sha256())).append('}');
        memberSeparator = ",";
      }
      result.append("}}");
      separator = ",";
    }
    return result.append("]}").toString();
  }

  public static void main(String[] arguments) throws IOException, NoSuchAlgorithmException {
    if (arguments.length < 3 || !"--output".equals(arguments[0])) {
      throw new IllegalArgumentException("expected --output NEW_JSON_FILE followed by explicit JAR paths");
    }
    List<Archive> archives = new ArrayList<>();
    var paths = new HashSet<String>();
    for (int index = 2; index < arguments.length; index++) {
      Archive archive = inspect(Path.of(arguments[index]));
      if (!paths.add(archive.path())) throw new IllegalArgumentException("duplicate JAR path");
      archives.add(archive);
    }
    Files.writeString(Path.of(arguments[1]), json(archives) + "\n", java.nio.file.StandardOpenOption.CREATE_NEW);
  }
}
