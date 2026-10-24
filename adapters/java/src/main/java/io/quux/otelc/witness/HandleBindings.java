package io.quux.otelc.witness;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.objectweb.asm.Opcodes;
import org.objectweb.asm.Type;
import org.objectweb.asm.tree.AbstractInsnNode;
import org.objectweb.asm.tree.FieldInsnNode;
import org.objectweb.asm.tree.MethodInsnNode;
import org.objectweb.asm.tree.VarInsnNode;

/** Offline binding component; this alone does not enable the planned handle runtime. */
public final class HandleBindings {
  public static final String PROFILE = "java-sync-private-handle-v1";
  private static final String HANDLE_OWNER = "java/lang/invoke/MethodHandle";
  private static final String HANDLE_DESCRIPTOR = "L" + HANDLE_OWNER + ";";
  private static final String WRAPPER = "wrapper";
  private static final String TARGET = "target";

  private HandleBindings() {}

  /** The field site is instruction zero in the exact original wrapper. */
  public record Route(String fieldOwner, String fieldName, String invokeDescriptor,
                      int referenceKind, String sha256) {}

  public record Compiled(Map<String, WitnessBindings.Binding> bindings, Route route) {
    public Compiled { bindings = Map.copyOf(bindings); }
  }

  /** Read original class bytes without resolving handles or loading application classes. */
  public static Compiled compile(Map<String, byte[]> classes,
                                 Map<String, WitnessBindings.Declaration> declarations) {
    var bindings = WitnessBindings.compileForProfile(classes, declarations, PROFILE);
    return new Compiled(bindings, validate(classes, declarations, bindings));
  }

  static Route validate(Map<String, byte[]> classes,
                        Map<String, WitnessBindings.Declaration> declarations,
                        Map<String, WitnessBindings.Binding> bindings) {
    var wrapper = declarations.get(WRAPPER);
    var target = declarations.get(TARGET);
    var wrapperClass = WitnessBindings.read(classes.get(wrapper.owner()));
    var targetClass = WitnessBindings.read(classes.get(target.owner()));
    var method = WitnessBindings.find(wrapperClass, wrapper);
    var targetMethod = WitnessBindings.find(targetClass, target);
    if ((targetClass.access & (Opcodes.ACC_INTERFACE | Opcodes.ACC_ANNOTATION | Opcodes.ACC_ABSTRACT)) != 0
        || (targetMethod.access & Opcodes.ACC_VARARGS) != 0) {
      throw new IllegalArgumentException("handle target requires an ordinary concrete class and fixed arity");
    }
    boolean isStatic = (targetMethod.access & Opcodes.ACC_STATIC) != 0;
    String descriptor = isStatic ? target.descriptor() : "(L" + target.owner() + ";" + target.descriptor().substring(1);
    if (!wrapper.descriptor().equals(descriptor) || !method.tryCatchBlocks.isEmpty()) {
      throw new IllegalArgumentException("handle wrapper requires exact target arguments and no catch blocks");
    }
    List<AbstractInsnNode> code = new ArrayList<>();
    for (var instruction : method.instructions) if (instruction.getOpcode() >= 0) code.add(instruction);
    Type[] arguments = Type.getArgumentTypes(descriptor);
    if (code.size() != arguments.length + 3 || !(code.getFirst() instanceof FieldInsnNode field)
        || field.getOpcode() != Opcodes.GETSTATIC || !field.owner.equals(wrapper.owner())
        || !field.desc.equals(HANDLE_DESCRIPTOR)) {
      throw new IllegalArgumentException("handle wrapper must begin with its own exact static handle field");
    }
    var fields = wrapperClass.fields.stream().filter(value -> value.name.equals(field.name)
        && value.desc.equals(HANDLE_DESCRIPTOR)).toList();
    int required = Opcodes.ACC_STATIC | Opcodes.ACC_FINAL;
    if (fields.size() != 1 || (fields.getFirst().access & (required | Opcodes.ACC_VOLATILE)) != required) {
      throw new IllegalArgumentException("handle field must be static final and nonvolatile");
    }
    new WitnessBindings.Declaration(field.owner, field.name, "()V");
    int slot = 0;
    for (int index = 0; index < arguments.length; index++) {
      if (!(code.get(index + 1) instanceof VarInsnNode load) || load.var != slot
          || load.getOpcode() != arguments[index].getOpcode(Opcodes.ILOAD)) {
        throw new IllegalArgumentException("handle wrapper must load its exact arguments in order");
      }
      slot += arguments[index].getSize();
    }
    if (slot > 254) throw new IllegalArgumentException("invokeExact arguments exceed the JVM slot bound");
    if (!(code.get(code.size() - 2) instanceof MethodInsnNode invoke)
        || invoke.getOpcode() != Opcodes.INVOKEVIRTUAL || invoke.itf
        || !invoke.owner.equals(HANDLE_OWNER) || !invoke.name.equals("invokeExact")
        || !invoke.desc.equals(descriptor)
        || code.getLast().getOpcode() != Type.getReturnType(descriptor).getOpcode(Opcodes.IRETURN)) {
      throw new IllegalArgumentException("handle wrapper requires one exact invokeExact and matching return");
    }
    int referenceKind = isStatic ? java.lang.invoke.MethodHandleInfo.REF_invokeStatic
                                : java.lang.invoke.MethodHandleInfo.REF_invokeVirtual;
    String digest = routeDigest(field.owner, field.name, descriptor, referenceKind, bindings);
    return new Route(field.owner, field.name, descriptor, referenceKind, digest);
  }

  static String routeDigest(String owner, String name, String descriptor, int referenceKind,
                            Map<String, WitnessBindings.Binding> bindings) {
    return WitnessBindings.digest(String.join("\n", PROFILE, "direct_method_handle",
        owner, name, HANDLE_DESCRIPTOR, "0", HANDLE_OWNER, "invokeExact", descriptor,
        Integer.toString(referenceKind), bindings.get(WRAPPER).probeId(), bindings.get(TARGET).probeId())
        .getBytes(StandardCharsets.UTF_8));
  }

  /** Separate closed offline format; ordinary v2 preparation deliberately refuses it. */
  public static void main(String[] arguments) throws IOException {
    var inputs = WitnessBindings.compilerInputs(arguments);
    Compiled compiled = compile(inputs.classes(), inputs.declarations());
    StringBuilder output = new StringBuilder("{\"schema_version\":3,\"kind\":\"otelc.java-witness-bindings\",\"profile\":\"")
        .append(PROFILE).append("\",\"bindings\":{");
    for (String role : WitnessPlan.ROLES) {
      if (!role.equals("envelope")) output.append(',');
      var binding = compiled.bindings().get(role);
      output.append('"').append(role).append("\":{\"probe_id\":\"").append(binding.probeId())
          .append("\",\"owner\":\"").append(binding.declaration().owner())
          .append("\",\"name\":\"").append(binding.declaration().name())
          .append("\",\"descriptor\":\"").append(binding.declaration().descriptor())
          .append("\",\"class_sha256\":\"").append(binding.classSha256())
          .append("\",\"method_sha256\":\"").append(binding.methodSha256()).append("\"}");
    }
    Route route = compiled.route();
    output.append("},\"route\":{\"kind\":\"direct_method_handle\",\"field_owner\":\"").append(route.fieldOwner())
        .append("\",\"field_name\":\"").append(route.fieldName()).append("\",\"field_descriptor\":\"").append(HANDLE_DESCRIPTOR)
        .append("\",\"site_instruction\":0,\"invoke_owner\":\"").append(HANDLE_OWNER)
        .append("\",\"invoke_name\":\"invokeExact\",\"invoke_descriptor\":\"").append(route.invokeDescriptor())
        .append("\",\"reference_kind\":").append(route.referenceKind())
        .append(",\"sha256\":\"").append(route.sha256()).append("\"}}\n");
    System.out.write(output.toString().getBytes(StandardCharsets.UTF_8));
  }
}
