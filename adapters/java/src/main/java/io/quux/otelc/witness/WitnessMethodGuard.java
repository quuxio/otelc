package io.quux.otelc.witness;

import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.Consumer;
import net.bytebuddy.asm.AsmVisitorWrapper;
import net.bytebuddy.description.field.FieldDescription;
import net.bytebuddy.description.field.FieldList;
import net.bytebuddy.description.method.MethodList;
import net.bytebuddy.description.type.TypeDescription;
import net.bytebuddy.implementation.Implementation;
import net.bytebuddy.pool.TypePool;
import org.objectweb.asm.ClassVisitor;
import org.objectweb.asm.MethodVisitor;
import org.objectweb.asm.Opcodes;
import org.objectweb.asm.tree.MethodNode;

/** Check actual incoming method streams before passing them to downstream probe advice. */
public final class WitnessMethodGuard extends AsmVisitorWrapper.AbstractBase {
  private final String owner;
  private final Map<String, WitnessBindings.Binding> expected;
  private final Consumer<WitnessBindings.Binding> matched;
  private final HandleBindings.Route route;

  public WitnessMethodGuard(List<WitnessBindings.Binding> bindings,
                            Consumer<WitnessBindings.Binding> matched) {
    this(bindings, matched, null);
  }

  public WitnessMethodGuard(List<WitnessBindings.Binding> bindings,
                            Consumer<WitnessBindings.Binding> matched, HandleBindings.Route route) {
    if (bindings.isEmpty()) throw new IllegalArgumentException("method guard requires bindings");
    owner = bindings.getFirst().declaration().owner();
    expected = new HashMap<>();
    for (WitnessBindings.Binding binding : bindings) {
      if (!binding.declaration().owner().equals(owner)) {
        throw new IllegalArgumentException("method guard requires one exact class owner");
      }
      String key = binding.declaration().name() + binding.declaration().descriptor();
      if (expected.putIfAbsent(key, binding) != null) {
        throw new IllegalArgumentException("duplicate guarded method");
      }
    }
    this.matched = java.util.Objects.requireNonNull(matched);
    this.route = route;
  }

  @Override public ClassVisitor wrap(TypeDescription instrumentedType, ClassVisitor visitor,
      Implementation.Context implementationContext, TypePool typePool,
      FieldList<FieldDescription.InDefinedShape> fields, MethodList<?> methods,
      int writerFlags, int readerFlags) {
    if (!instrumentedType.getInternalName().equals(owner)) {
      throw new IllegalArgumentException("guarded type disagrees with frozen owner");
    }
    return new ClassVisitor(Opcodes.ASM9, visitor) {
      private final Set<String> seen = new HashSet<>();

      @Override public void visit(int version, int access, String name, String signature,
                                  String superName, String[] interfaces) {
        if (!owner.equals(name)) throw new IllegalStateException("incoming class owner changed");
        super.visit(version, access, name, signature, superName, interfaces);
      }

      @Override public MethodVisitor visitMethod(int access, String name, String descriptor,
                                                  String signature, String[] exceptions) {
        MethodVisitor downstream = super.visitMethod(access, name, descriptor, signature, exceptions);
        String key = name + descriptor;
        WitnessBindings.Binding binding = expected.get(key);
        if (binding == null) return downstream;
        if (downstream == null) throw new IllegalStateException("downstream omitted guarded method");
        if (!seen.add(key)) throw new IllegalStateException("duplicate incoming guarded method");
        return new MethodNode(Opcodes.ASM9, access, name, descriptor, signature, exceptions) {
          @Override public void visitEnd() {
            if (!WitnessBindings.methodDigest(binding.declaration(), this).equals(binding.methodSha256())) {
              throw new IllegalStateException("incoming method differs from frozen witness binding: " + binding.role());
            }
            accept(route != null && binding.role().equals("wrapper") ? operandProbe(downstream) : downstream);
          }
        };
      }

      @Override public void visitEnd() {
        if (!seen.equals(expected.keySet())) throw new IllegalStateException("missing incoming guarded method");
        super.visitEnd();
        // Matching proves the input to this visitor, not JVM definition or probe execution.
        expected.values().forEach(matched);
      }
    };
  }

  private MethodVisitor operandProbe(MethodVisitor downstream) {
    return new MethodVisitor(Opcodes.ASM9, downstream) {
      private int observed;

      @Override public void visitFieldInsn(int opcode, String owner, String name, String descriptor) {
        super.visitFieldInsn(opcode, owner, name, descriptor);
        if (opcode != Opcodes.GETSTATIC || !owner.equals(route.fieldOwner()) || !name.equals(route.fieldName())
            || !descriptor.equals("Ljava/lang/invoke/MethodHandle;") || ++observed != 1) {
          throw new IllegalStateException("handle operand site disagrees with frozen route");
        }
        super.visitInsn(Opcodes.DUP);
        super.visitMethodInsn(Opcodes.INVOKESTATIC, "io/quux/otelc/witness/probe/Observer", "handle",
            "(Ljava/lang/invoke/MethodHandle;)V", false);
      }

      @Override public void visitMaxs(int stack, int locals) { super.visitMaxs(stack + 1, locals); }

      @Override public void visitEnd() {
        if (observed != 1) throw new IllegalStateException("missing handle operand observation");
        super.visitEnd();
      }
    };
  }
}
