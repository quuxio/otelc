package io.quux.otelc.witness;

import static net.bytebuddy.matcher.ElementMatchers.named;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import net.bytebuddy.ByteBuddy;
import net.bytebuddy.asm.Advice;
import net.bytebuddy.description.type.TypeDescription;
import net.bytebuddy.dynamic.loading.ByteArrayClassLoader;
import org.junit.jupiter.api.Test;
import org.objectweb.asm.ClassReader;
import org.objectweb.asm.ClassVisitor;
import org.objectweb.asm.ClassWriter;
import org.objectweb.asm.Opcodes;
import org.objectweb.asm.Type;
import org.objectweb.asm.tree.InsnNode;

class WitnessMethodGuardTests {
  public static class HandleFixture {
    public static final RuntimeException FAILURE = new RuntimeException("original");
    private static final java.lang.invoke.MethodHandle HANDLE = handle();
    private static java.lang.invoke.MethodHandle handle() {
      try { return java.lang.invoke.MethodHandles.lookup().findStatic(HandleFixture.class, "target",
          java.lang.invoke.MethodType.methodType(int.class, int.class)); }
      catch (ReflectiveOperationException error) { throw new AssertionError(error); }
    }
    private static int target(int value) { if (value < 0) throw FAILURE; return value + 1; }
    public static int wrapper(int value) throws Throwable { return (int) HANDLE.invokeExact(value); }
    public static void rendezvous() {}
    public static int envelope(int value) throws Throwable { rendezvous(); return wrapper(value); }
  }

  public static class Fixture {
    public static int target(int value) { if (value < 0) throw new IllegalArgumentException(); return value + 1; }
    public static int wrapper(int value) { return target(value); }
    public static void rendezvous() { Thread.onSpinWait(); }
    public static int envelope(int value) { rendezvous(); return wrapper(value); }
  }

  public static class AddedAdvice {
    @Advice.OnMethodExit
    public static void exit(@Advice.Return(readOnly = false) int value) { value += 5; }
  }

  static final String OWNER = Type.getInternalName(Fixture.class);

  static byte[] bytes() throws IOException {
    try (var stream = Fixture.class.getResourceAsStream("/" + OWNER + ".class")) {
      return stream.readAllBytes();
    }
  }

  static List<WitnessBindings.Binding> bindings() throws IOException {
    Map<String, WitnessBindings.Declaration> declarations = new java.util.HashMap<>();
    for (String role : List.of("envelope", "rendezvous", "wrapper", "target")) {
      declarations.put(role, new WitnessBindings.Declaration(OWNER, role, role.equals("rendezvous") ? "()V" : "(I)I"));
    }
    return new ArrayList<>(WitnessBindings.compile(Map.of(OWNER, bytes()), declarations).values());
  }

  static ClassVisitor visitor(WitnessMethodGuard guard, ClassWriter writer) {
    return guard.wrap(TypeDescription.ForLoadedType.of(Fixture.class), writer, null, null, null, null, 0, 0);
  }

  @Test void actualHandleOperandProbePreservesVerifierResultsAndThrowableIdentity() throws Exception {
    String owner = Type.getInternalName(HandleFixture.class);
    byte[] original;
    try (var input = HandleFixture.class.getResourceAsStream("/" + owner + ".class")) { original = input.readAllBytes(); }
    Map<String, WitnessBindings.Declaration> declarations = new java.util.HashMap<>();
    for (String role : WitnessPlan.ROLES) declarations.put(role,
        new WitnessBindings.Declaration(owner, role, role.equals("rendezvous") ? "()V" : "(I)I"));
    var compiled = HandleBindings.compile(Map.of(owner, original), declarations);
    List<WitnessBindings.Binding> matched = new ArrayList<>();
    var guard = new WitnessMethodGuard(new ArrayList<>(compiled.bindings().values()), matched::add, compiled.route());
    byte[] transformed = new ByteBuddy().redefine(HandleFixture.class).visit(guard).make().getBytes();
    var loader = new ByteArrayClassLoader.ChildFirst(HandleFixture.class.getClassLoader(), Map.of(HandleFixture.class.getName(), transformed));
    Class<?> actual = loader.loadClass(HandleFixture.class.getName());
    var envelope = actual.getMethod("envelope", int.class);
    assertEquals(8, envelope.invoke(null, 7));
    var failed = assertThrows(java.lang.reflect.InvocationTargetException.class, () -> envelope.invoke(null, -1));
    assertTrue(failed.getCause() == actual.getField("FAILURE").get(null));
    assertEquals(4, matched.size());
    var wrong = new HandleBindings.Route(owner, "OTHER", "(I)I", 6, "a".repeat(64));
    var invalid = new ByteBuddy().redefine(HandleFixture.class).visit(
        new WitnessMethodGuard(new ArrayList<>(compiled.bindings().values()), ignored -> {}, wrong));
    assertThrows(IllegalStateException.class, invalid::make);
    var missing = new ByteBuddy().redefine(Fixture.class).visit(new WitnessMethodGuard(bindings(), ignored -> {},
        new HandleBindings.Route(OWNER, "MISSING", "(I)I", 6, "a".repeat(64))));
    assertThrows(IllegalStateException.class, missing::make);
  }

  @Test void validatesActualByteBuddyInputAndPreservesExecutableAdvice() throws ReflectiveOperationException, IOException {
    List<WitnessBindings.Binding> matched = new ArrayList<>();
    WitnessMethodGuard guard = new WitnessMethodGuard(bindings(), matched::add);
    byte[] transformed = new ByteBuddy().redefine(Fixture.class)
        .visit(Advice.to(AddedAdvice.class).on(named("target"))).visit(guard).make().getBytes();
    assertEquals(4, matched.size());
    var loader = new ByteArrayClassLoader.ChildFirst(Fixture.class.getClassLoader(), Map.of(Fixture.class.getName(), transformed));
    Class<?> actual = loader.loadClass(Fixture.class.getName());
    assertEquals(7, actual.getMethod("envelope", int.class).invoke(null, 1));
    var target = actual.getMethod("target", int.class);
    var error = assertThrows(java.lang.reflect.InvocationTargetException.class, () -> target.invoke(null, -1));
    assertTrue(error.getCause() instanceof IllegalArgumentException);
  }

  @Test void rejectsAdviceWhichRunsBeforeTheOriginalCodeGuard() throws IOException {
    List<WitnessBindings.Binding> matched = new ArrayList<>();
    WitnessMethodGuard guard = new WitnessMethodGuard(bindings(), matched::add);
    var builder = new ByteBuddy().redefine(Fixture.class).visit(guard)
        .visit(Advice.to(AddedAdvice.class).on(named("target")));
    assertThrows(IllegalStateException.class, builder::make);
    assertTrue(matched.isEmpty());
  }

  @Test void rejectsChangedMissingAndDuplicateIncomingMethodsBeforeReportingMatches() throws IOException {
    for (String mutation : List.of("changed", "missing", "duplicate")) {
      var type = WitnessBindings.read(bytes());
      var target = type.methods.stream().filter(method -> method.name.equals("target")).findFirst().orElseThrow();
      switch (mutation) {
        case "changed" -> target.instructions.insert(new InsnNode(Opcodes.NOP));
        case "missing" -> type.methods.remove(target);
        case "duplicate" -> type.methods.add(target);
        default -> throw new AssertionError(mutation);
      }
      List<WitnessBindings.Binding> matched = new ArrayList<>();
      ClassWriter output = new ClassWriter(0);
      ClassVisitor guarded = visitor(new WitnessMethodGuard(bindings(), matched::add), output);
      assertThrows(IllegalStateException.class, () -> type.accept(guarded));
      assertTrue(matched.isEmpty());
    }
  }

  @Test void preservesOriginalCodeAndDebugMetadataAndRejectsWrongType() throws IOException {
    List<WitnessBindings.Binding> matched = new ArrayList<>();
    WitnessMethodGuard guard = new WitnessMethodGuard(bindings(), matched::add);
    ClassWriter output = new ClassWriter(0);
    new ClassReader(bytes()).accept(visitor(guard, output), 0);
    var original = WitnessBindings.read(bytes());
    var checked = WitnessBindings.read(output.toByteArray());
    assertEquals(original.methods.size(), checked.methods.size());
    for (var method : original.methods) {
      var declaration = new WitnessBindings.Declaration(OWNER, method.name.equals("<init>") ? "constructor" : method.name, method.desc);
      var actual = checked.methods.stream().filter(value -> value.name.equals(method.name) && value.desc.equals(method.desc)).findFirst().orElseThrow();
      assertEquals(WitnessBindings.methodDigest(declaration, method), WitnessBindings.methodDigest(declaration, actual));
      assertEquals(fullMethodDigest(method), WitnessBindings.methodDigest(declaration, method));
    }
    var fullOriginal = new org.objectweb.asm.tree.ClassNode();
    var fullChecked = new org.objectweb.asm.tree.ClassNode();
    new ClassReader(bytes()).accept(fullOriginal, 0);
    new ClassReader(output.toByteArray()).accept(fullChecked, 0);
    assertEquals(fullOriginal.sourceFile, fullChecked.sourceFile);
    for (var method : fullOriginal.methods) {
      var actual = fullChecked.methods.stream().filter(value -> value.name.equals(method.name) && value.desc.equals(method.desc)).findFirst().orElseThrow();
      assertEquals(fullMethodDigest(method), fullMethodDigest(actual));
    }
    assertTrue(fullOriginal.methods.stream().flatMap(value -> java.util.Arrays.stream(value.instructions.toArray()))
        .anyMatch(value -> value instanceof org.objectweb.asm.tree.LineNumberNode));
    var wrongType = TypeDescription.ForLoadedType.of(String.class);
    assertThrows(IllegalArgumentException.class, () -> guard.wrap(wrongType, output, null, null, null, null, 0, 0));
    ClassVisitor wrongOwner = visitor(guard, output);
    assertThrows(IllegalStateException.class, () -> wrongOwner.visit(Opcodes.V21, 0, "Other", null, "java/lang/Object", null));
    List<WitnessBindings.Binding> empty = List.of();
    assertThrows(IllegalArgumentException.class, () -> new WitnessMethodGuard(empty, matched::add));
    var first = bindings().getFirst();
    List<WitnessBindings.Binding> duplicate = List.of(first, first);
    assertThrows(IllegalArgumentException.class, () -> new WitnessMethodGuard(duplicate, matched::add));
    var foreign = new WitnessBindings.Binding("foreign", new WitnessBindings.Declaration("Other", "target", "(I)I"),
        first.classSha256(), first.methodSha256(), first.probeId());
    List<WitnessBindings.Binding> mixed = List.of(first, foreign);
    assertThrows(IllegalArgumentException.class, () -> new WitnessMethodGuard(mixed, matched::add));
  }

  @Test void refusesADownstreamVisitorWhichOmitsGuardedMethods() throws IOException {
    List<WitnessBindings.Binding> matched = new ArrayList<>();
    WitnessMethodGuard guard = new WitnessMethodGuard(bindings(), matched::add);
    ClassVisitor ignored = new ClassVisitor(Opcodes.ASM9) {};
    ClassVisitor guarded = guard.wrap(TypeDescription.ForLoadedType.of(Fixture.class), ignored, null, null, null, null, 0, 0);
    ClassReader reader = new ClassReader(bytes());
    assertThrows(IllegalStateException.class, () -> reader.accept(guarded, 0));
    assertTrue(matched.isEmpty());
  }

  static String fullMethodDigest(org.objectweb.asm.tree.MethodNode method) {
    ClassWriter writer = new ClassWriter(0);
    writer.visit(Opcodes.V21, Opcodes.ACC_PUBLIC, OWNER, null, "java/lang/Object", null);
    method.accept(writer.visitMethod(method.access, method.name, method.desc, method.signature,
        method.exceptions.toArray(String[]::new)));
    writer.visitEnd();
    return WitnessBindings.digest(writer.toByteArray());
  }
}
