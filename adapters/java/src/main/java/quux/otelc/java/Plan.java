package quux.otelc.java;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.regex.Pattern;

/** The Rust resolver remains the authority for shared policy and byte globs. */
public final class Plan {
  final JsonObject data;
  final Selection sources;
  final Selection functions;
  public Plan(Path filename) throws IOException { this(JsonParser.parseString(Files.readString(filename)).getAsJsonObject()); }
  public Plan(JsonObject data) {
    this.data = data;
    if (!data.get("language").getAsString().equals("java") || !data.get("execution_available").getAsBoolean()) throw new IllegalArgumentException("agent requires an executable Java policy");
    var propagation=section("propagation");
    if(propagation!=null && propagation.get("tasks").getAsBoolean() && !TraceStore.enabled(this)) throw new IllegalArgumentException("Java task context requires traces");
    sources = new Selection(section("source_matchers"));
    functions = new Selection(section("function_matchers"));
  }
  JsonObject section(String name) { return data.getAsJsonObject(name); }
  int integer(String section, String name) { return section(section).get(name).getAsInt(); }
  String string(String section, String name) { return section(section).get(name).getAsString(); }
  boolean bool(String section, String name) { return section(section).get(name).getAsBoolean(); }
  Path controlSocket() { var value = section("runtime").get("control_socket"); return value == null || value.isJsonNull() ? null : Path.of(value.getAsString()); }
  static final class Selection {
    private final List<Pattern> include;
    private final List<Pattern> exclude;
    Selection(JsonObject json) { include = patterns(json, "include"); exclude = patterns(json, "exclude"); }
    private static List<Pattern> patterns(JsonObject json, String name) {
      return json.getAsJsonArray(name).asList().stream().map(value -> Pattern.compile(value.getAsString().replaceFirst("^\\(\\?-u\\)", ""), Pattern.DOTALL)).toList();
    }
    boolean accepts(String name, boolean annotated) {
      String bytes = new String(name.getBytes(StandardCharsets.UTF_8), StandardCharsets.ISO_8859_1);
      return (annotated || include.stream().anyMatch(value -> value.matcher(bytes).matches())) && exclude.stream().noneMatch(value -> value.matcher(bytes).matches());
    }
  }
}
