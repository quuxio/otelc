package io.quux.otelc.witness;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.PrintStream;
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
import org.objectweb.asm.tree.InsnNode;
import org.objectweb.asm.tree.JumpInsnNode;
import org.objectweb.asm.tree.LabelNode;
import org.objectweb.asm.tree.LdcInsnNode;
import org.objectweb.asm.tree.LookupSwitchInsnNode;
import org.objectweb.asm.tree.MethodInsnNode;
import org.objectweb.asm.tree.MethodNode;
import org.objectweb.asm.tree.TableSwitchInsnNode;
import org.objectweb.asm.tree.TryCatchBlockNode;

class WitnessBindingsTests {
  static final String OWNER = Type.getInternalName(Fixture.class);
  static final Map<String, WitnessBindings.Declaration> DECLARATIONS = Map.of(
      "envelope", declaration("envelope", "(I)I"), "rendezvous", declaration("rendezvous", "()V"),
      "wrapper", declaration("wrapper", "(I)I"), "target", declaration("target", "(I)I"));

  static class Fixture {
    static int target(int value) { if (value < 0) throw new IllegalArgumentException(); return value + 1; }
    static int target(long value) { return (int) value + 2; }
    static int wrapper(int value) { return target(value); }
    static void rendezvous() { Thread.onSpinWait(); }
    static int envelope(int value) { int observed = Math.abs(value); rendezvous(); return wrapper(observed); }
    int instance(int value) { return value; }
    static int instanceWrapper(Fixture fixture, int value) { return fixture.instance(value); }
    static int instanceEnvelope(Fixture fixture, int value) { rendezvous(); return instanceWrapper(fixture, value); }
    static int callbackEnvelope(int value) { rendezvous(); return wrapper(Math.abs(value)); }
    static int bypassEnvelope(int value) {
      if (value == 0) return wrapper(value);
      rendezvous();
      return wrapper(value);
    }
    static int catchingEnvelope(int value) {
      try { rendezvous(); return wrapper(value); }
      catch (IllegalArgumentException error) { return -1; }
    }
    static int duplicateWrapper(int value) { target(value); return target(value); }
    static int unicodeWrapper(int value) { return Δ.μέτρο(value); }
    static int unicodeEnvelope(int value) { rendezvous(); return unicodeWrapper(value); }
  }

  static class Δ {
    static int μέτρο(int value) { return value; }
  }

  static WitnessBindings.Declaration declaration(String method, String descriptor) {
    return new WitnessBindings.Declaration(OWNER, method, descriptor);
  }

  static byte[] original() throws IOException {
    try (var stream = Fixture.class.getResourceAsStream("/" + OWNER + ".class")) {
      return stream.readAllBytes();
    }
  }

  static Map<String, WitnessBindings.Binding> compile(byte[] bytes) {
    return WitnessBindings.compile(Map.of(OWNER, bytes), DECLARATIONS);
  }

  static byte[] change(String role, Consumer<MethodNode> mutation) throws IOException {
    ClassNode type = WitnessBindings.read(original());
    mutation.accept(WitnessBindings.find(type, DECLARATIONS.get(role)));
    ClassWriter output = new ClassWriter(0);
    type.accept(output);
    return output.toByteArray();
  }

  @Test void bindsExactOverloadsAndCompleteOriginalClasses() throws IOException {
    Map<String, WitnessBindings.Binding> first = compile(original());
    assertEquals(first, compile(original()));
    assertEquals(4, first.values().stream().map(WitnessBindings.Binding::probeId).distinct().count());
    byte[] changed = change("target", method -> method.instructions.insert(new InsnNode(Opcodes.NOP)));
    Map<String, WitnessBindings.Binding> second = compile(changed);
    assertNotEquals(first.get("target").methodSha256(), second.get("target").methodSha256());
    assertEquals(first.get("wrapper").methodSha256(), second.get("wrapper").methodSha256());
    assertNotEquals(first.get("wrapper").probeId(), second.get("wrapper").probeId());
    assertEquals(WitnessBindings.digest(original()), first.get("target").classSha256());
    assertThrows(UnsupportedOperationException.class, () -> first.clear());
  }

  @Test void supportsOrdinaryReceiverArgumentsWithoutInvokingApplicationCode() throws IOException {
    Map<String, WitnessBindings.Declaration> instance = new HashMap<>(DECLARATIONS);
    instance.put("target", declaration("instance", "(I)I"));
    instance.put("wrapper", declaration("instanceWrapper", "(L" + OWNER + ";I)I"));
    instance.put("envelope", declaration("instanceEnvelope", "(L" + OWNER + ";I)I"));
    assertEquals(4, WitnessBindings.compile(Map.of(OWNER, original()), instance).size());
  }

  @Test void supportsUnicodeJavaNamesAndRejectsInvalidJvmDescriptors() throws IOException {
    String owner = Type.getInternalName(Δ.class);
    Map<String, WitnessBindings.Declaration> declarations = new HashMap<>(DECLARATIONS);
    declarations.put("target", new WitnessBindings.Declaration(owner, "μέτρο", "(I)I"));
    declarations.put("wrapper", declaration("unicodeWrapper", "(I)I"));
    declarations.put("envelope", declaration("unicodeEnvelope", "(I)I"));
    try (var stream = Δ.class.getResourceAsStream("/" + owner + ".class")) {
      assertEquals("μέτρο", WitnessBindings.compile(Map.of(OWNER, original(), owner, stream.readAllBytes()), declarations)
          .get("target").declaration().name());
    }
    for (String descriptor : new String[] {"(" + "J".repeat(128) + ")V", "()" + "[".repeat(256) + "I",
        "(L;)V", "(Ljava/lang/Object)V", "(Lbad.name;)V", "([)V", "(I", "(", "()VI"}) {
      assertThrows(IllegalArgumentException.class, () -> declaration("x", descriptor), descriptor);
    }
    assertEquals("(" + "I".repeat(255) + ")V", declaration("x", "(" + "I".repeat(255) + ")V").descriptor());
    assertEquals("()[I", declaration("x", "()[I").descriptor());
    assertThrows(IllegalArgumentException.class, () -> declaration("x\u0001", "()V"));
  }

  @Test void refusesMissingDuplicateMalformedAndWrongOwnerDeclarations() throws IOException {
    Map<String, byte[]> noClasses = Map.of();
    assertThrows(IllegalArgumentException.class, () -> WitnessBindings.compile(noClasses, DECLARATIONS));
    Map<String, WitnessBindings.Declaration> declarations = new HashMap<>(DECLARATIONS);
    declarations.remove("wrapper");
    assertThrows(IllegalArgumentException.class, () -> WitnessBindings.compile(noClasses, declarations));
    declarations.put("wrapper", DECLARATIONS.get("target"));
    assertThrows(IllegalArgumentException.class, () -> WitnessBindings.compile(noClasses, declarations));
    declarations.put("wrapper", declaration("absent", "()V"));
    byte[] bytes = original();
    Map<String, byte[]> classes = Map.of(OWNER, bytes);
    assertThrows(IllegalArgumentException.class, () -> WitnessBindings.compile(classes, declarations));
    declarations.put("wrapper", new WitnessBindings.Declaration("Other", "wrapper", "(I)I"));
    Map<String, byte[]> wrongOwner = Map.of(OWNER, bytes, "Other", bytes);
    assertThrows(IllegalArgumentException.class,
        () -> WitnessBindings.compile(wrongOwner, declarations));
    for (String descriptor : new String[] {"", "I", "()", "(V)V", "()[V", "()Vjunk", "(Lbad//name;)V"}) {
      assertThrows(IllegalArgumentException.class, () -> declaration("target", descriptor), descriptor);
    }
    assertThrows(IllegalArgumentException.class, () -> declaration("<init>", "()V"));
    assertThrows(IllegalArgumentException.class, () -> new WitnessBindings.Declaration("bad//name", "x", "()V"));
    assertThrows(IllegalArgumentException.class, () -> new WitnessBindings.Declaration(null, "x", "()V"));
    assertThrows(IllegalArgumentException.class, () -> new WitnessBindings.Declaration(OWNER, null, "()V"));
    assertThrows(IllegalArgumentException.class, () -> declaration("x", null));
  }

  @Test void refusesNativeSyntheticInstanceOrSynchronizedControlMethods() throws IOException {
    for (int flags : new int[] {Opcodes.ACC_NATIVE, Opcodes.ACC_ABSTRACT, Opcodes.ACC_SYNTHETIC,
        Opcodes.ACC_BRIDGE, Opcodes.ACC_SYNCHRONIZED}) {
      byte[] bytes = change("wrapper", method -> method.access |= flags);
      assertThrows(IllegalArgumentException.class, () -> compile(bytes));
    }
    byte[] instance = change("envelope", method -> method.access &= ~Opcodes.ACC_STATIC);
    assertThrows(IllegalArgumentException.class, () -> compile(instance));
    byte[] empty = change("rendezvous", method -> method.instructions.clear());
    assertThrows(IllegalArgumentException.class, () -> compile(empty));
    Map<String, WitnessBindings.Declaration> declarations = new HashMap<>(DECLARATIONS);
    declarations.put("rendezvous", declaration("target", "(J)I"));
    byte[] bytes = original();
    Map<String, byte[]> classes = Map.of(OWNER, bytes);
    assertThrows(IllegalArgumentException.class, () -> WitnessBindings.compile(classes, declarations));
  }

  @Test void refusesActualJavacCompiledUnsafeHarnessesAndWrongOverload() throws IOException {
    byte[] bytes = original();
    Map<String, byte[]> classes = Map.of(OWNER, bytes);
    for (String method : new String[] {"callbackEnvelope", "bypassEnvelope", "catchingEnvelope"}) {
      Map<String, WitnessBindings.Declaration> declarations = new HashMap<>(DECLARATIONS);
      declarations.put("envelope", declaration(method, "(I)I"));
      assertThrows(IllegalArgumentException.class, () -> WitnessBindings.compile(classes, declarations));
    }
    Map<String, WitnessBindings.Declaration> declarations = new HashMap<>(DECLARATIONS);
    declarations.put("wrapper", declaration("duplicateWrapper", "(I)I"));
    assertThrows(IllegalArgumentException.class, () -> WitnessBindings.compile(classes, declarations));
    declarations.put("wrapper", DECLARATIONS.get("wrapper"));
    declarations.put("target", declaration("target", "(J)I"));
    assertThrows(IllegalArgumentException.class, () -> WitnessBindings.compile(classes, declarations));
  }

  @Test void refusesWrapperCallbacksCatchesDuplicateCallsAndWrongStaticDispatch() throws IOException {
    for (Consumer<MethodNode> mutation : java.util.List.<Consumer<MethodNode>>of(
        method -> method.instructions.insert(new MethodInsnNode(Opcodes.INVOKESTATIC, "Other", "callback", "()V", false)),
        method -> method.instructions.insert(new InsnNode(Opcodes.RETURN)),
        method -> method.instructions.insert(new LdcInsnNode(Type.getType("Ljava/lang/String;"))),
        method -> method.instructions.insertBefore(method.instructions.getLast(), new InsnNode(Opcodes.NOP)),
        method -> method.instructions.add(new InsnNode(Opcodes.NOP)),
        method -> method.instructions.clear(),
        method -> method.tryCatchBlocks.add(new TryCatchBlockNode(new LabelNode(), new LabelNode(), new LabelNode(), "java/lang/Throwable")),
        method -> targetCall(method).setOpcode(Opcodes.INVOKEVIRTUAL),
        method -> method.instructions.insertBefore(targetCall(method), targetCall(method).clone(Map.of())))) {
      byte[] bytes = change("wrapper", mutation);
      assertThrows(IllegalArgumentException.class, () -> compile(bytes));
    }
  }

  static MethodInsnNode targetCall(MethodNode method) {
    for (var instruction : method.instructions) {
      if (instruction instanceof MethodInsnNode call) return call;
    }
    throw new AssertionError("fixture has no invocation");
  }

  @Test void refusesInterveningCallbacksAndControlFlowBypass() throws IOException {
    for (Consumer<MethodNode> mutation : java.util.List.<Consumer<MethodNode>>of(
        method -> method.instructions.insert(rendezvousCall(method), new MethodInsnNode(Opcodes.INVOKESTATIC, "Other", "callback", "()V", false)),
        method -> rendezvousCall(method).setOpcode(Opcodes.INVOKEVIRTUAL),
        method -> method.instructions.add(new InsnNode(Opcodes.NOP)),
        method -> method.instructions.clear(),
        method -> {
          for (var instruction : method.instructions.toArray()) {
            if (instruction.getOpcode() == Opcodes.IRETURN) method.instructions.remove(instruction);
          }
        },
        method -> method.tryCatchBlocks.add(new TryCatchBlockNode(new LabelNode(), new LabelNode(), new LabelNode(), "java/lang/Throwable")),
        method -> injectJump(method, 0), method -> injectJump(method, 1), method -> injectJump(method, 2))) {
      byte[] bytes = change("envelope", mutation);
      assertThrows(IllegalArgumentException.class, () -> compile(bytes));
    }
  }

  static void injectJump(MethodNode method, int kind) {
    LabelNode tail = new LabelNode();
    method.instructions.insert(rendezvousCall(method), tail);
    method.instructions.insert(switch (kind) {
      case 0 -> new JumpInsnNode(Opcodes.GOTO, tail);
      case 1 -> new TableSwitchInsnNode(0, 0, tail, tail);
      default -> new LookupSwitchInsnNode(tail, new int[] {1}, new LabelNode[] {tail});
    });
  }

  static MethodInsnNode rendezvousCall(MethodNode method) {
    for (var instruction : method.instructions) {
      if (instruction instanceof MethodInsnNode call && call.name.equals("rendezvous")) return call;
    }
    throw new AssertionError("fixture has no rendezvous");
  }

  @Test void commandOutputsBoundIdentitiesAndRejectsConflictingInputs(@TempDir Path directory) throws IOException {
    Path file = directory.resolve("Fixture.class");
    Files.write(file, original());
    String[] arguments = new String[16];
    int offset = 0;
    for (String role : java.util.List.of("envelope", "rendezvous", "wrapper", "target")) {
      var declaration = DECLARATIONS.get(role);
      arguments[offset++] = file.toString(); arguments[offset++] = OWNER;
      arguments[offset++] = declaration.name(); arguments[offset++] = declaration.descriptor();
    }
    ByteArrayOutputStream output = new ByteArrayOutputStream();
    PrintStream stdout = System.out;
    try (PrintStream capture = new PrintStream(output, true, StandardCharsets.UTF_8)) {
      System.setOut(capture);
      WitnessBindings.main(arguments);
    } finally {
      System.setOut(stdout);
    }
    String json = output.toString(StandardCharsets.UTF_8);
    assertTrue(json.contains("\"profile\":\"java-sync-action-v1\""));
    assertTrue(json.contains(compile(original()).get("wrapper").probeId()));
    assertTrue(json.contains("\"descriptor\":\"(I)I\""));
    Path changed = directory.resolve("changed.class");
    Files.write(changed, change("target", method -> method.instructions.insert(new InsnNode(Opcodes.NOP))));
    arguments[4] = changed.toString();
    assertThrows(IllegalArgumentException.class, () -> WitnessBindings.main(arguments));
    assertThrows(IllegalArgumentException.class, () -> WitnessBindings.main(new String[0]));
  }
}
