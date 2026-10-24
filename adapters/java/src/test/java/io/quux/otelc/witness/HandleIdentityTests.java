package io.quux.otelc.witness;

import static org.junit.jupiter.api.Assertions.*;

import java.lang.invoke.MethodHandleInfo;
import java.lang.invoke.MethodHandles;
import java.lang.invoke.MethodType;
import java.util.Set;
import org.junit.jupiter.api.Test;
import org.objectweb.asm.Type;

class HandleIdentityTests {
  static class Owner {
    private int target(int value) { return value + 1; }
    private int other(int value) { return value + 1; }
    private static int fixed(int value) { return value; }
    private static int identity(int value) { return value; }
    private static int variadic(Object... values) { return values.length; }
    private static boolean stack() {
      return StackWalker.getInstance(Set.of(StackWalker.Option.RETAIN_CLASS_REFERENCE,
          StackWalker.Option.SHOW_HIDDEN_FRAMES)).walk(frames -> frames.anyMatch(HandleIdentity::machinery));
    }
  }

  @Test void exactDirectPrivateOperandsMatchWithoutInvokingTheTarget() throws Exception {
    var lookup = MethodHandles.privateLookupIn(Owner.class, MethodHandles.lookup());
    String owner = Type.getInternalName(Owner.class);
    for (boolean isStatic : new boolean[]{false, true}) {
      String name = isStatic ? "fixed" : "target";
      var method = MethodType.methodType(int.class, int.class);
      var handle = isStatic ? lookup.findStatic(Owner.class, name, method) : lookup.findVirtual(Owner.class, name, method);
      var target = new WitnessBindings.Declaration(owner, name, "(I)I");
      var route = new HandleBindings.Route(owner, "HANDLE", handle.type().toMethodDescriptorString(),
          isStatic ? MethodHandleInfo.REF_invokeStatic : MethodHandleInfo.REF_invokeVirtual, "a".repeat(64));
      assertTrue(HandleIdentity.matches(handle, Owner.class, route, target));
      assertFalse(HandleIdentity.matches(null, Owner.class, route, target));
      assertFalse(HandleIdentity.matches(handle, String.class, route, target));
      assertFalse(HandleIdentity.matches(handle, getClass(), route, target));
      assertFalse(HandleIdentity.matches(handle, Owner.class,
          new HandleBindings.Route(owner, "HANDLE", "()V", route.referenceKind(), route.sha256()), target));
      assertFalse(HandleIdentity.matches(handle, Owner.class,
          new HandleBindings.Route(owner, "HANDLE", route.invokeDescriptor(), 7, route.sha256()), target));
      assertFalse(HandleIdentity.matches(handle, Owner.class, route, new WitnessBindings.Declaration(owner, "other", "(I)I")));
      assertFalse(HandleIdentity.matches(handle, Owner.class, route, new WitnessBindings.Declaration(owner, name, "(I)J")));
      var adapted = MethodHandles.filterReturnValue(handle, lookup.findStatic(Owner.class, "identity", method));
      assertFalse(HandleIdentity.matches(adapted, Owner.class, route, target));
      if (!isStatic) assertFalse(HandleIdentity.matches(handle.bindTo(new Owner()), Owner.class, route, target));
    }
    var collector = lookup.findStatic(Owner.class, "variadic", MethodType.methodType(int.class, Object[].class));
    assertFalse(HandleIdentity.matches(collector, Owner.class,
        new HandleBindings.Route(owner, "HANDLE", collector.type().toMethodDescriptorString(), 6, "a".repeat(64)),
        new WitnessBindings.Declaration(owner, "variadic", "([Ljava/lang/Object;)I")));
  }

  @Test void onlyActualBootstrapJavaBaseHandleFramesAreMachinery() throws Throwable {
    var handle = MethodHandles.privateLookupIn(Owner.class, MethodHandles.lookup()).findStatic(
        Owner.class, "stack", MethodType.methodType(boolean.class));
    assertTrue((boolean) handle.invokeExact());
    boolean ordinary = StackWalker.getInstance(StackWalker.Option.RETAIN_CLASS_REFERENCE)
        .walk(frames -> HandleIdentity.machinery(frames.findFirst().orElseThrow()));
    assertFalse(ordinary);
  }
}
