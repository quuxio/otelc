package quux.otelc.java;

import java.io.IOException;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.objectweb.asm.ClassReader;
import org.objectweb.asm.Opcodes;

/** Per-transformation hierarchy metadata; never load or define application classes. */
final class FrameTypes {
  private static final String OBJECT = "java/lang/Object";
  private record Metadata(String parent, List<String> interfaces, boolean isInterface) {}
  private final ClassLoader loader;
  private final Map<String, Metadata> types = new HashMap<>();
  FrameTypes(ClassReader current, ClassLoader loader) { this.loader = loader; remember(current); }
  private void remember(ClassReader reader) {
    types.put(reader.getClassName(), new Metadata(reader.getSuperName(), List.of(reader.getInterfaces()), (reader.getAccess() & Opcodes.ACC_INTERFACE) != 0));
  }
  private Metadata metadata(String name) {
    if (!types.containsKey(name)) {
      if (types.size() >= 4096) throw new IllegalArgumentException("Java frame hierarchy exceeds metadata bound");
      try (var input = loader == null ? ClassLoader.getSystemResourceAsStream(name + ".class") : loader.getResourceAsStream(name + ".class")) {
        if (input == null) throw new IllegalArgumentException("Java frame class resource unavailable");
        byte[] bytes = input.readNBytes(16777217);
        if (bytes.length > 16777216) throw new IllegalArgumentException("Java frame class resource exceeds byte bound");
        var reader = new ClassReader(bytes);
        if (!reader.getClassName().equals(name)) throw new IllegalArgumentException("Java frame resource has wrong class identity");
        remember(reader);
      } catch (IOException failure) { throw new IllegalArgumentException("cannot read Java frame class resource", failure); }
    }
    return types.get(name);
  }
  private static boolean array(String name) { return name.startsWith("["); }
  private static String component(String name) {
    String value = name.substring(1);
    return value.startsWith("L") ? value.substring(1, value.length() - 1) : value;
  }
  private boolean assignable(String target, String value, Set<String> visited) {
    if (target.equals(value)) return true;
    if (target.equals(OBJECT)) return true;
    if (array(value)) {
      if (target.equals("java/lang/Cloneable") || target.equals("java/io/Serializable")) return true;
      if (!array(target)) return false;
      if (target.substring(1).length() == 1 || value.substring(1).length() == 1) return target.equals(value);
      return assignable(component(target), component(value), new HashSet<>());
    }
    if (array(target) || !visited.add(value)) return false;
    var type = metadata(value);
    if (type.parent() != null && assignable(target, type.parent(), visited)) return true;
    return type.interfaces().stream().anyMatch(parent -> assignable(target, parent, visited));
  }
  String common(String left, String right) {
    if (assignable(left, right, new HashSet<>())) return left;
    if (assignable(right, left, new HashSet<>())) return right;
    if (array(left) && array(right)) {
      if (left.substring(1).length() == 1 || right.substring(1).length() == 1) return OBJECT;
      String first = component(left), second = component(right);
      String parent = common(first, second);
      return "[" + (array(parent) ? parent : "L" + parent + ";");
    }
    if (array(left) || array(right) || metadata(left).isInterface() || metadata(right).isInterface()) return OBJECT;
    var visited = new HashSet<String>();
    String parent = left;
    while (visited.add(parent)) {
      parent = metadata(parent).parent();
      if (parent == null) return OBJECT;
      if (assignable(parent, right, new HashSet<>())) return parent;
    }
    throw new IllegalArgumentException("cyclic Java frame hierarchy");
  }
}
