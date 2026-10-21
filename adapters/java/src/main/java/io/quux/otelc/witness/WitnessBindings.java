package io.quux.otelc.witness;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.objectweb.asm.ClassReader;
import org.objectweb.asm.ClassWriter;
import org.objectweb.asm.Opcodes;
import org.objectweb.asm.tree.AbstractInsnNode;
import org.objectweb.asm.tree.ClassNode;
import org.objectweb.asm.tree.FrameNode;
import org.objectweb.asm.tree.JumpInsnNode;
import org.objectweb.asm.tree.LabelNode;
import org.objectweb.asm.tree.LdcInsnNode;
import org.objectweb.asm.tree.LineNumberNode;
import org.objectweb.asm.tree.LookupSwitchInsnNode;
import org.objectweb.asm.tree.MethodInsnNode;
import org.objectweb.asm.tree.MethodNode;
import org.objectweb.asm.tree.TableSwitchInsnNode;

/** Compile exact method bindings for the proposed synchronous action capability. */
public final class WitnessBindings {
  public static final String PROFILE = "java-sync-action-v1";
  private static final String ENVELOPE = "envelope";
  private static final String RENDEZVOUS = "rendezvous";
  private static final String WRAPPER = "wrapper";
  private static final String TARGET = "target";
  private static final List<String> ROLE_ORDER = List.of(ENVELOPE, RENDEZVOUS, WRAPPER, TARGET);
  private static final Set<String> ROLES = Set.copyOf(ROLE_ORDER);
  private static final int FORBIDDEN = Opcodes.ACC_ABSTRACT | Opcodes.ACC_NATIVE
      | Opcodes.ACC_SYNTHETIC | Opcodes.ACC_BRIDGE;

  private WitnessBindings() {}

  /** The owner is a JVM internal name; descriptor includes the exact return type. */
  public record Declaration(String owner, String name, String descriptor) {
    public Declaration {
      if (!internalName(owner) || !identifier(name) || !methodDescriptor(descriptor)) {
        throw new IllegalArgumentException("invalid witness declaration");
      }
    }

    private static boolean identifier(String text) {
      if (text == null || text.isEmpty() || !Character.isJavaIdentifierStart(text.codePointAt(0))) return false;
      return text.codePoints().allMatch(value -> Character.isJavaIdentifierPart(value) && !Character.isISOControl(value));
    }

    private static boolean internalName(String text) {
      if (text == null) return false;
      for (String segment : text.split("/", -1)) {
        if (!identifier(segment)) return false;
      }
      return true;
    }

    private static boolean methodDescriptor(String text) {
      if (text == null || !text.startsWith("(")) return false;
      int index = 1;
      int slots = 0;
      while (index < text.length() && text.charAt(index) != ')') {
        int end = valueEnd(text, index);
        if (end < 0) return false;
        slots += text.charAt(index) == 'J' || text.charAt(index) == 'D' ? 2 : 1;
        if (slots > 255) return false;
        index = end;
      }
      if (index >= text.length() - 1) return false;
      index++;
      return text.charAt(index) == 'V' ? index == text.length() - 1 : valueEnd(text, index) == text.length();
    }

    private static int valueEnd(String text, int index) {
      int dimensions = 0;
      while (index < text.length() && text.charAt(index) == '[') {
        if (++dimensions > 255) return -1;
        index++;
      }
      if (index == text.length()) return -1;
      char kind = text.charAt(index);
      if ("BCDFIJSZ".indexOf(kind) >= 0) return index + 1;
      if (kind != 'L') return -1;
      int end = text.indexOf(';', index);
      return end > index && internalName(text.substring(index + 1, end)) ? end + 1 : -1;
    }

    boolean matches(MethodInsnNode instruction) {
      return owner.equals(instruction.owner) && name.equals(instruction.name)
          && descriptor.equals(instruction.desc);
    }
  }

  /** IDs bind role, complete original class bytes and canonical method code. */
  public record Binding(String role, Declaration declaration, String classSha256,
                        String methodSha256, String probeId) {}

  /** Validate all four roles together; do not derive a runtime capability from this result. */
  public static Map<String, Binding> compile(Map<String, byte[]> classes,
                                              Map<String, Declaration> declarations) {
    if (!declarations.keySet().equals(ROLES)) {
      throw new IllegalArgumentException("exactly envelope, rendezvous, wrapper and target are required");
    }
    if (Set.copyOf(declarations.values()).size() != 4) {
      throw new IllegalArgumentException("witness roles must identify distinct methods");
    }
    Map<String, Binding> bindings = new LinkedHashMap<>();
    Map<String, MethodNode> methods = new LinkedHashMap<>();
    for (String role : ROLE_ORDER) {
      Declaration declaration = declarations.get(role);
      byte[] bytes = classes.get(declaration.owner());
      if (bytes == null) throw new IllegalArgumentException("missing witness class: " + declaration.owner());
      ClassNode type = read(bytes);
      if (!type.name.equals(declaration.owner())) {
        throw new IllegalArgumentException("class bytes disagree with declared owner");
      }
      MethodNode method = find(type, declaration);
      if ((method.access & FORBIDDEN) != 0 || method.instructions.size() == 0) {
        throw new IllegalArgumentException("witness method must have ordinary executable bytecode");
      }
      if (!role.equals(TARGET) && (method.access & (Opcodes.ACC_STATIC | Opcodes.ACC_SYNCHRONIZED)) != Opcodes.ACC_STATIC) {
        throw new IllegalArgumentException("control methods must be static and unsynchronised");
      }
      String classHash = digest(bytes);
      String methodHash = methodDigest(declaration, method);
      String probe = digest(String.join("\n", PROFILE, role, declaration.owner(), declaration.name(),
          declaration.descriptor(), classHash, methodHash).getBytes(StandardCharsets.UTF_8));
      bindings.put(role, new Binding(role, declaration, classHash, methodHash, probe));
      methods.put(role, method);
    }
    if (!declarations.get(RENDEZVOUS).descriptor().equals("()V")) {
      throw new IllegalArgumentException("rendezvous must be static ()V");
    }
    validateWrapper(methods.get(WRAPPER), declarations.get(TARGET), methods.get(TARGET));
    validateEnvelope(methods.get(ENVELOPE), declarations.get(RENDEZVOUS), declarations.get(WRAPPER));
    return Map.copyOf(bindings);
  }

  static ClassNode read(byte[] bytes) {
    ClassNode node = new ClassNode(Opcodes.ASM9);
    new ClassReader(bytes).accept(node, ClassReader.SKIP_DEBUG | ClassReader.SKIP_FRAMES);
    return node;
  }

  static MethodNode find(ClassNode type, Declaration declaration) {
    List<MethodNode> matches = type.methods.stream().filter(method -> method.name.equals(declaration.name())
        && method.desc.equals(declaration.descriptor())).toList();
    if (matches.size() != 1) throw new IllegalArgumentException("missing or ambiguous witness method");
    return matches.getFirst();
  }

  /** Canonical original method representation for subsequent transformation checks. */
  static String methodDigest(Declaration declaration, MethodNode method) {
    MethodNode canonical = new MethodNode(Opcodes.ASM9, method.access, method.name, method.desc,
        method.signature, method.exceptions.toArray(String[]::new));
    method.accept(canonical);
    canonical.parameters = null;
    canonical.localVariables = null;
    for (AbstractInsnNode instruction : canonical.instructions.toArray()) {
      if (instruction instanceof FrameNode || instruction instanceof LineNumberNode) {
        canonical.instructions.remove(instruction);
      }
    }
    ClassWriter writer = new ClassWriter(0);
    writer.visit(Opcodes.V21, Opcodes.ACC_PUBLIC, declaration.owner(), null, "java/lang/Object", null);
    canonical.accept(writer.visitMethod(canonical.access, canonical.name, canonical.desc, canonical.signature,
        canonical.exceptions.toArray(String[]::new)));
    writer.visitEnd();
    return digest(writer.toByteArray());
  }

  private static void validateWrapper(MethodNode method, Declaration target, MethodNode targetMethod) {
    if (!method.tryCatchBlocks.isEmpty()) throw new IllegalArgumentException("wrapper cannot catch target errors");
    int stage = 0;
    for (AbstractInsnNode instruction : method.instructions) {
      if (instruction.getOpcode() >= 0) stage = wrapperStep(instruction, target, targetMethod, stage);
    }
    if (stage != 2) throw new IllegalArgumentException("wrapper requires one target call and return");
  }

  private static int wrapperStep(AbstractInsnNode instruction, Declaration target,
                                 MethodNode targetMethod, int stage) {
    if (stage == 2) throw new IllegalArgumentException("wrapper has code after return");
    if (instruction instanceof MethodInsnNode call && target.matches(call)) {
      if (stage != 0) throw new IllegalArgumentException("wrapper must invoke target exactly once");
      if ((call.getOpcode() == Opcodes.INVOKESTATIC) != ((targetMethod.access & Opcodes.ACC_STATIC) != 0)) {
        throw new IllegalArgumentException("target invocation disagrees with static binding");
      }
      return 1;
    }
    if (stage == 1 && isReturn(instruction)) return 2;
    if (stage == 0 && isArgument(instruction)) return 0;
    throw new IllegalArgumentException("wrapper body must only load arguments, invoke target and return");
  }

  private static void validateEnvelope(MethodNode method, Declaration rendezvous, Declaration wrapper) {
    if (!method.tryCatchBlocks.isEmpty()) throw new IllegalArgumentException("envelope cannot catch action errors");
    int stage = 0;
    Set<LabelNode> tailLabels = new java.util.HashSet<>();
    for (AbstractInsnNode instruction : method.instructions) {
      if (stage > 0 && instruction instanceof LabelNode label) tailLabels.add(label);
      if (instruction.getOpcode() >= 0) stage = envelopeStep(instruction, rendezvous, wrapper, stage);
    }
    if (stage != 3) throw new IllegalArgumentException("envelope requires rendezvous, wrapper and return");
    for (AbstractInsnNode instruction : method.instructions) {
      if (entersTail(instruction, tailLabels)) {
        throw new IllegalArgumentException("control flow can bypass rendezvous or repeat action");
      }
    }
  }

  private static int envelopeStep(AbstractInsnNode instruction, Declaration rendezvous,
                                  Declaration wrapper, int stage) {
    if (instruction instanceof MethodInsnNode call && rendezvous.matches(call)) {
      if (stage != 0 || call.getOpcode() != Opcodes.INVOKESTATIC) {
        throw new IllegalArgumentException("one static rendezvous call is required");
      }
      return 1;
    }
    if (instruction instanceof MethodInsnNode call && wrapper.matches(call)) {
      if (stage != 1 || call.getOpcode() != Opcodes.INVOKESTATIC) {
        throw new IllegalArgumentException("wrapper must directly follow rendezvous");
      }
      return 2;
    }
    if (stage == 0 || stage == 1 && isArgument(instruction)) return stage;
    if (stage == 2 && isReturn(instruction)) return 3;
    throw new IllegalArgumentException("envelope action tail must only load arguments, call wrapper and return");
  }

  private static boolean entersTail(AbstractInsnNode instruction, Set<LabelNode> tail) {
    if (instruction instanceof JumpInsnNode jump) return tail.contains(jump.label);
    if (instruction instanceof TableSwitchInsnNode table) {
      return tail.contains(table.dflt) || table.labels.stream().anyMatch(tail::contains);
    }
    if (instruction instanceof LookupSwitchInsnNode lookup) {
      return tail.contains(lookup.dflt) || lookup.labels.stream().anyMatch(tail::contains);
    }
    return false;
  }

  private static boolean isArgument(AbstractInsnNode instruction) {
    int opcode = instruction.getOpcode();
    return opcode >= Opcodes.ILOAD && opcode <= Opcodes.ALOAD
        || opcode >= Opcodes.ACONST_NULL && opcode <= Opcodes.DCONST_1
        || opcode == Opcodes.BIPUSH || opcode == Opcodes.SIPUSH
        || instruction instanceof LdcInsnNode constant && (constant.cst instanceof String
            || constant.cst instanceof Integer || constant.cst instanceof Long
            || constant.cst instanceof Float || constant.cst instanceof Double);
  }

  private static boolean isReturn(AbstractInsnNode instruction) {
    return instruction.getOpcode() >= Opcodes.IRETURN && instruction.getOpcode() <= Opcodes.RETURN;
  }

  static String digest(byte[] bytes) {
    try {
      return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes));
    } catch (NoSuchAlgorithmException impossible) {
      throw new IllegalStateException("JVM requires SHA-256", impossible);
    }
  }

  /** Offline diagnostic compiler: four ordered groups of classfile/owner/name/descriptor. */
  public static void main(String[] arguments) throws IOException {
    if (arguments.length != 16) throw new IllegalArgumentException("expected four classfile/owner/name/descriptor groups");
    Map<String, byte[]> classes = new LinkedHashMap<>();
    Map<String, Declaration> declarations = new LinkedHashMap<>();
    for (int index = 0; index < ROLE_ORDER.size(); index++) {
      int offset = index * 4;
      byte[] bytes = Files.readAllBytes(Path.of(arguments[offset]));
      String owner = arguments[offset + 1];
      byte[] previous = classes.putIfAbsent(owner, bytes);
      if (previous != null && !java.util.Arrays.equals(previous, bytes)) {
        throw new IllegalArgumentException("conflicting class bytes for one owner");
      }
      declarations.put(ROLE_ORDER.get(index), new Declaration(owner, arguments[offset + 2], arguments[offset + 3]));
    }
    Map<String, Binding> result = compile(classes, declarations);
    StringBuilder output = new StringBuilder("{\"schema_version\":1,\"kind\":\"otelc.java-witness-bindings\",\"profile\":\"")
        .append(PROFILE).append("\",\"bindings\":{");
    for (String role : ROLE_ORDER) {
      if (!role.equals(ENVELOPE)) output.append(',');
      Binding binding = result.get(role);
      output.append('"').append(role).append("\":{\"probe_id\":\"").append(binding.probeId())
          .append("\",\"owner\":\"").append(binding.declaration().owner())
          .append("\",\"name\":\"").append(binding.declaration().name())
          .append("\",\"descriptor\":\"").append(binding.declaration().descriptor())
          .append("\",\"class_sha256\":\"").append(binding.classSha256())
          .append("\",\"method_sha256\":\"").append(binding.methodSha256()).append("\"}");
    }
    System.out.write(output.append("}}\n").toString().getBytes(StandardCharsets.UTF_8));
  }
}
