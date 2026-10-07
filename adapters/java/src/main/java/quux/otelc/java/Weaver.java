package quux.otelc.java;

import java.lang.instrument.ClassFileTransformer;
import java.lang.instrument.Instrumentation;
import java.security.ProtectionDomain;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.objectweb.asm.Attribute;
import org.objectweb.asm.ByteVector;
import org.objectweb.asm.ClassReader;
import org.objectweb.asm.ClassVisitor;
import org.objectweb.asm.ClassWriter;
import org.objectweb.asm.Label;
import org.objectweb.asm.MethodVisitor;
import org.objectweb.asm.Opcodes;
import org.objectweb.asm.Type;
import org.objectweb.asm.commons.AdviceAdapter;
import org.objectweb.asm.commons.Method;
import org.objectweb.asm.tree.AnnotationNode;
import org.objectweb.asm.tree.ClassNode;
import org.objectweb.asm.tree.MethodNode;

/** Function-body timing; constructor admission follows the base constructor. */
final class Weaver implements ClassFileTransformer {
  private static final String MARKER = "QuuxOtelcGenerated";
  private static final Type PROBES = Type.getType(Probes.class);
  private static final Method ENTER = new Method("enter", "(Ljava/lang/String;)J");
  private static final Method EXIT = new Method("exit", "(JZ)V");
  private final Plan plan;
  private final Sources sources;
  private final Telemetry runtime;
  private final Instrumentation instrumentation;
  Weaver(Plan plan, Sources sources, Telemetry runtime, Instrumentation instrumentation) { this.plan = plan; this.sources = sources; this.runtime = runtime; this.instrumentation = instrumentation; }
  record Function(String name, boolean selected, String descriptor) {}
  private static boolean annotation(List<AnnotationNode> annotations, String name) {
    return annotations != null && annotations.stream().anyMatch(value -> value.desc.equals("Lotelc/" + name + ";") || value.desc.equals("LOtelc" + name + ";") || value.desc.endsWith("/Otelc" + name + ";") || value.desc.endsWith("$Otelc" + name + ";"));
  }
  List<Function> inventory(ClassNode node) {
    boolean sourceSelected = sources.name(node.name, node.sourceFile) != null;
    var result = new ArrayList<Function>();
    for (var method : node.methods) {
      if (method.name.equals("<clinit>") || (method.access & (Opcodes.ACC_ABSTRACT | Opcodes.ACC_NATIVE | Opcodes.ACC_SYNTHETIC | Opcodes.ACC_BRIDGE)) != 0) continue;
      String name = node.name.replace('/', '.') + "." + method.name + "(" + String.join(",", java.util.Arrays.stream(Type.getArgumentTypes(method.desc)).map(Type::getClassName).toList()) + ")";
      boolean read = plan.bool("annotations", "read_existing");
      boolean exclude = read && (annotation(method.visibleAnnotations, "Exclude") || annotation(method.invisibleAnnotations, "Exclude"));
      boolean include = read && (annotation(method.visibleAnnotations, "Instrument") || annotation(method.invisibleAnnotations, "Instrument"));
      result.add(new Function(name, sourceSelected && !exclude && plan.functions.accepts(name, include), method.desc));
    }
    return result;
  }
  byte[] weave(byte[] original, ClassLoader loader) {
    var reader = new ClassReader(original); var node = new ClassNode(); reader.accept(node, ClassReader.EXPAND_FRAMES);
    if (node.attrs != null && node.attrs.stream().anyMatch(attribute -> attribute.type.equals(MARKER))) throw new IllegalArgumentException("class is already instrumented");
    // Trace mode must observe rejected calls so an enclosing tree cannot export incomplete data.
    var selected = inventory(node).stream().filter(Function::selected).filter(value -> runtime == null || runtime.traces != null || runtime.register(value.name())).toList();
    if (selected.isEmpty()) return null;
    var writer = new ClassWriter(reader, ClassWriter.COMPUTE_FRAMES | ClassWriter.COMPUTE_MAXS) {
      @Override protected String getCommonSuperClass(String left, String right) {
        try {
          var first = Class.forName(left.replace('/', '.'), false, loader); var second = Class.forName(right.replace('/', '.'), false, loader);
          if (first.isAssignableFrom(second)) return left; if (second.isAssignableFrom(first)) return right;
          if (first.isInterface() || second.isInterface()) return "java/lang/Object";
          do { first = first.getSuperclass(); } while (!first.isAssignableFrom(second));
          return first.getName().replace('.', '/');
        } catch (ClassNotFoundException failure) { throw new IllegalArgumentException("cannot resolve Java frame types", failure); }
      }
    };
    var visitor = new ClassVisitor(Opcodes.ASM9, writer) {
      @Override public MethodVisitor visitMethod(int access, String name, String descriptor, String signature, String[] exceptions) {
        var output = super.visitMethod(access, name, descriptor, signature, exceptions);
        var function = selected.stream().filter(value -> value.descriptor().equals(descriptor) && value.name().substring(0, value.name().indexOf('(')).endsWith("." + name)).findFirst();
        if (function.isEmpty()) return output;
        if (plan.bool("annotations", "inject_generated")) output.visitAnnotation("Lotelc/Instrument;", false).visitEnd();
        return new AdviceAdapter(Opcodes.ASM9, output, access, name, descriptor) {
          private final Label start = new Label(); private final Label end = new Label(); private final Label handler = new Label();
          private int token; private boolean admitted;
          @Override protected void onMethodEnter() {
            push(function.get().name()); invokeStatic(PROBES, ENTER); token = newLocal(Type.LONG_TYPE); storeLocal(token); mark(start); admitted = true;
          }
          @Override protected void onMethodExit(int opcode) { if (opcode != ATHROW && admitted) { loadLocal(token); push(false); invokeStatic(PROBES, EXIT); } }
          @Override public void visitMaxs(int stack, int locals) {
            if (admitted) {
              mark(end); visitTryCatchBlock(start, end, handler, "java/lang/Throwable"); mark(handler);
              loadLocal(token); push(true); invokeStatic(PROBES, EXIT); throwException();
            }
            super.visitMaxs(stack, locals);
          }
        };
      }
      @Override public void visitEnd() {
        visitAttribute(new Attribute(MARKER) {
          @Override protected ByteVector write(ClassWriter writer, byte[] code, int length, int stack, int locals) { return new ByteVector(); }
        });
        super.visitEnd();
      }
    };
    reader.accept(visitor, ClassReader.EXPAND_FRAMES); return writer.toByteArray();
  }
  @Override public byte[] transform(Module module, ClassLoader loader, String name, Class<?> redefining, ProtectionDomain domain, byte[] original) {
    if (name == null || loader == null || loader == ClassLoader.getPlatformClassLoader() || name.startsWith("quux/otelc/java/") || name.startsWith("quux/otelc/shaded/") || redefining != null) return null;
    try {
      var reader = new ClassReader(original); var node = new ClassNode(); reader.accept(node, ClassReader.SKIP_CODE);
      if (sources.name(node.name, node.sourceFile) == null || inventory(node).stream().noneMatch(Function::selected)) return null;
      if (Class.forName(Probes.class.getName(), false, loader) != Probes.class) throw new IllegalArgumentException("isolated classloader cannot access agent probes");
      if (module != null && module.isNamed() && instrumentation != null) instrumentation.redefineModule(module, Set.of(Probes.class.getModule()), Map.of(), Map.of(), Set.of(), Map.of());
      return weave(original, loader);
    } catch (Throwable failure) {
      if (runtime != null) runtime.lose("unsupported_class");
      System.err.println("otelc Java: selected class could not be instrumented: " + name);
      return null;
    }
  }
}
