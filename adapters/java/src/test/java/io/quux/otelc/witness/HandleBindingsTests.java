package io.quux.otelc.witness;

import static org.junit.jupiter.api.Assertions.*;

import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.lang.invoke.MethodHandle;
import java.lang.invoke.MethodHandleInfo;
import java.lang.invoke.MethodHandles;
import java.lang.invoke.MethodType;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.Map;
import java.util.function.Consumer;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.objectweb.asm.ClassWriter;
import org.objectweb.asm.Opcodes;
import org.objectweb.asm.Type;
import org.objectweb.asm.tree.ClassNode;
import org.objectweb.asm.tree.FieldInsnNode;
import org.objectweb.asm.tree.InsnNode;
import org.objectweb.asm.tree.MethodInsnNode;
import org.objectweb.asm.tree.VarInsnNode;

class HandleBindingsTests {
  static final String OWNER = Type.getInternalName(Fixture.class);

  static class Fixture {
    static final MethodHandle HANDLE = unavailable();
    private static MethodHandle unavailable() { throw new AssertionError("offline compiler initialised application"); }
    private int target(long value, double other) { return (int) (value + other); }
    private static long staticTarget(long value) { return value; }
    static int wrapper(Fixture receiver, long value, double other) throws Throwable {
      return (int) HANDLE.invokeExact(receiver, value, other);
    }
    static long staticWrapper(long value) throws Throwable { return (long) HANDLE.invokeExact(value); }
    static void rendezvous() {}
    static int envelope(Fixture receiver, long value, double other) throws Throwable {
      rendezvous(); return wrapper(receiver, value, other);
    }
    static long staticEnvelope(long value) throws Throwable { rendezvous(); return staticWrapper(value); }
  }

  private static Map<String, WitnessBindings.Declaration> declarations(boolean isStatic) {
    String descriptor = isStatic ? "(J)J" : "(L" + OWNER + ";JD)I";
    return Map.of("envelope", new WitnessBindings.Declaration(OWNER, isStatic ? "staticEnvelope" : "envelope", descriptor),
        "rendezvous", new WitnessBindings.Declaration(OWNER, "rendezvous", "()V"),
        "wrapper", new WitnessBindings.Declaration(OWNER, isStatic ? "staticWrapper" : "wrapper", descriptor),
        "target", new WitnessBindings.Declaration(OWNER, isStatic ? "staticTarget" : "target", isStatic ? "(J)J" : "(JD)I"));
  }

  private static byte[] original() throws Exception {
    try (var input = Fixture.class.getResourceAsStream("/" + OWNER + ".class")) { return input.readAllBytes(); }
  }

  private static byte[] change(Consumer<ClassNode> edit) throws Exception {
    var type = WitnessBindings.read(original()); edit.accept(type);
    var writer = new ClassWriter(0); type.accept(writer); return writer.toByteArray();
  }

  private static HandleBindings.Compiled compile(byte[] bytes, boolean isStatic) {
    return HandleBindings.compile(Map.of(OWNER, bytes), declarations(isStatic));
  }

  @Test void bindsPrivateInstanceAndStaticTargetsWithoutInitialisingTheirClasses() throws Exception {
    for (boolean isStatic : new boolean[]{false, true}) {
      var compiled = compile(original(), isStatic);
      assertEquals(compiled, compile(original(), isStatic));
      assertEquals(4, compiled.bindings().size());
      assertEquals(OWNER, compiled.route().fieldOwner());
      assertEquals("HANDLE", compiled.route().fieldName());
      assertEquals(isStatic ? MethodHandleInfo.REF_invokeStatic : MethodHandleInfo.REF_invokeVirtual,
                   compiled.route().referenceKind());
      assertEquals(declarations(isStatic).get("wrapper").descriptor(), compiled.route().invokeDescriptor());
      assertEquals(64, compiled.route().sha256().length());
      assertThrows(UnsupportedOperationException.class, () -> compiled.bindings().clear());
      byte[] bytes = original();
      assertThrows(IllegalArgumentException.class, () -> WitnessBindings.compile(Map.of(OWNER, bytes), declarations(isStatic)));
    }
    assertThrows(IllegalArgumentException.class, () -> WitnessBindings.compileForProfile(Map.of(), Map.of(), "future"));
  }

  @Test void bindsWholeClassAndBothRouteMethodsAndUsesDistinctProbeDomain() throws Exception {
    var original = compile(original(), false);
    byte[] changed = change(type -> WitnessBindings.find(type, declarations(false).get("target"))
        .instructions.insert(new InsnNode(Opcodes.NOP)));
    var second = compile(changed, false);
    assertNotEquals(original.route().sha256(), second.route().sha256());
    assertNotEquals(original.bindings().get("wrapper").probeId(), second.bindings().get("wrapper").probeId());
    var direct = WitnessBindingsTests.compile(WitnessBindingsTests.original());
    for (var binding : original.bindings().values()) {
      assertNotEquals(direct.get(binding.role()).probeId(), binding.probeId());
    }
  }

  @Test void refusesUnfrozenFieldWrongRoutesArgumentsDescriptorsAndCatchBodies() throws Exception {
    java.util.List<Consumer<ClassNode>> edits = java.util.List.of(
        type -> type.fields.stream().filter(f -> f.name.equals("HANDLE")).findFirst().orElseThrow().access &= ~Opcodes.ACC_FINAL,
        type -> type.fields.stream().filter(f -> f.name.equals("HANDLE")).findFirst().orElseThrow().access |= Opcodes.ACC_VOLATILE,
        type -> type.fields.removeIf(f -> f.name.equals("HANDLE")),
        type -> type.fields.add(type.fields.stream().filter(f -> f.name.equals("HANDLE")).findFirst().orElseThrow()),
        type -> ((FieldInsnNode) WitnessBindings.find(type, declarations(false).get("wrapper")).instructions.getFirst()).owner = "Other",
        type -> ((FieldInsnNode) WitnessBindings.find(type, declarations(false).get("wrapper")).instructions.getFirst()).desc = "Ljava/lang/Object;",
        type -> WitnessBindings.find(type, declarations(false).get("wrapper")).instructions.insert(new InsnNode(Opcodes.NOP)),
        type -> ((VarInsnNode) WitnessBindings.find(type, declarations(false).get("wrapper")).instructions.get(1)).var = 2,
        type -> WitnessBindings.find(type, declarations(false).get("target")).access |= Opcodes.ACC_VARARGS,
        type -> type.access |= Opcodes.ACC_ABSTRACT,
        type -> { for (var insn : WitnessBindings.find(type, declarations(false).get("wrapper")).instructions) if (insn instanceof MethodInsnNode invoke) invoke.name = "invoke"; },
        type -> { for (var insn : WitnessBindings.find(type, declarations(false).get("wrapper")).instructions) if (insn instanceof MethodInsnNode invoke) invoke.desc = "(Ljava/lang/Object;JD)I"; },
        type -> { var method = WitnessBindings.find(type, declarations(false).get("wrapper")); method.instructions.set(method.instructions.getLast(), new InsnNode(Opcodes.ARETURN)); }
    );
    for (var edit : edits) {
      byte[] bytes = change(edit);
      assertThrows(IllegalArgumentException.class, () -> compile(bytes, false));
    }
    var declarations = new HashMap<>(declarations(false));
    declarations.put("wrapper", new WitnessBindings.Declaration(OWNER, "staticWrapper", "(J)J"));
    byte[] bytes = original();
    assertThrows(IllegalArgumentException.class, () -> HandleBindings.compile(Map.of(OWNER, bytes), declarations));
  }

  @Test void actualJdkPrivateVirtualHandleIsDirectUnboundAndKeepsTheExactType() throws Throwable {
    class Owner { private int increment(int value) { return value + 1; } }
    var lookup = MethodHandles.privateLookupIn(Owner.class, MethodHandles.lookup());
    var handle = lookup.findVirtual(Owner.class, "increment", MethodType.methodType(int.class, int.class));
    var info = lookup.revealDirect(handle);
    assertEquals(MethodHandleInfo.REF_invokeVirtual, info.getReferenceKind());
    assertEquals(Owner.class, info.getDeclaringClass());
    assertEquals(MethodType.methodType(int.class, int.class), info.getMethodType());
    assertEquals(MethodType.methodType(int.class, Owner.class, int.class), handle.type());
    Owner receiver = new Owner();
    assertEquals(8, (int) handle.invokeExact(receiver, 7));
    assertThrows(IllegalArgumentException.class, () -> lookup.revealDirect(handle.bindTo(receiver)));
    assertThrows(IllegalArgumentException.class, () -> lookup.revealDirect(handle.asType(MethodType.methodType(Object.class, Object.class, Object.class))));
  }

  @Test void separateOfflineCommandEmitsTheClosedHandleRoute(@TempDir Path root) throws Exception {
    Path input = root.resolve("Fixture.class"); Files.write(input, original());
    var arguments = new java.util.ArrayList<String>();
    for (String role : WitnessPlan.ROLES) {
      var declaration = declarations(false).get(role);
      arguments.addAll(java.util.List.of(input.toString(), declaration.owner(), declaration.name(), declaration.descriptor()));
    }
    var output = new ByteArrayOutputStream(); PrintStream previous = System.out;
    try {
      System.setOut(new PrintStream(output, true, StandardCharsets.UTF_8));
      HandleBindings.main(arguments.toArray(String[]::new));
    } finally { System.setOut(previous); }
    String json = output.toString(StandardCharsets.UTF_8);
    assertTrue(json.startsWith("{\"schema_version\":3,\"kind\":\"otelc.java-witness-bindings\",\"profile\":\"java-sync-private-handle-v1\""));
    assertTrue(json.contains("\"site_instruction\":0"));
    assertTrue(json.contains("\"invoke_name\":\"invokeExact\""));
    assertTrue(json.contains("\"sha256\":\"" + compile(original(), false).route().sha256() + "\""));
    assertThrows(IllegalArgumentException.class, () -> HandleBindings.main(new String[0]));
  }
}
