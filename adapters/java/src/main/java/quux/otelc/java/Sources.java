package quux.otelc.java;

import com.sun.source.util.JavacTask;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import javax.tools.ToolProvider;

/** Associate class SourceFile metadata with original paths using the JDK parser. */
final class Sources {
  private final Map<String, List<String>> files = new HashMap<>();
  Sources(Path root, Plan plan) throws IOException {
    var compiler = ToolProvider.getSystemJavaCompiler();
    if (compiler == null) throw new IllegalArgumentException("Java instrumentation requires a full JDK 21+");
    var filenames = new ArrayList<Path>();
    try (var paths = Files.walk(root)) {
      paths.filter(Files::isRegularFile).filter(value -> value.toString().endsWith(".java")).forEach(value -> {
        String relative = root.relativize(value).toString().replace('\\', '/');
        if (!relative.startsWith("adapters/") && !relative.contains("node_modules/") && !relative.startsWith(".venv/") && !relative.contains("/target/") && !relative.startsWith("target/") && plan.sources.accepts(relative, false)) filenames.add(value);
      });
    }
    if (filenames.isEmpty()) return;
    try (var manager = compiler.getStandardFileManager(null, null, java.nio.charset.StandardCharsets.UTF_8)) {
      var diagnostics = new javax.tools.DiagnosticCollector<javax.tools.JavaFileObject>();
      var task = (JavacTask) compiler.getTask(null, manager, diagnostics, List.of("-proc:none"), null, manager.getJavaFileObjectsFromPaths(filenames));
      for (var unit : task.parse()) {
        var filename = Path.of(unit.getSourceFile().toUri());
        String relative = root.relativize(filename).toString().replace('\\', '/');
        String prefix = unit.getPackageName() == null ? "" : unit.getPackageName().toString().replace('.', '/') + "/";
        String key = prefix + filename.getFileName();
        files.computeIfAbsent(key, ignored -> new ArrayList<>()).add(relative);
      }
      var error = diagnostics.getDiagnostics().stream().filter(value -> value.getKind() == javax.tools.Diagnostic.Kind.ERROR).findFirst();
      if (error.isPresent()) throw new IllegalArgumentException("selected Java source is invalid: " + error.get().getCode());
    }
    if (files.values().stream().anyMatch(value -> value.size() > 1)) throw new IllegalArgumentException("ambiguous Java package/SourceFile paths; narrow source filters");
  }
  String name(String className, String sourceFile) {
    if (sourceFile == null) return null;
    String key = className.substring(0, className.lastIndexOf('/') + 1) + sourceFile;
    var candidates = files.get(key);
    return candidates == null ? null : candidates.getFirst();
  }
}
