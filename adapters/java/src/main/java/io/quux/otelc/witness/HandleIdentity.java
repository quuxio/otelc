package io.quux.otelc.witness;

import java.lang.invoke.MethodHandle;
import java.lang.invoke.MethodHandles;

/** Inspect only the actual JDK operand and immutable loaded target metadata. */
final class HandleIdentity {
  private HandleIdentity() {}

  static boolean matches(MethodHandle operand, Class<?> owner, HandleBindings.Route route,
                          WitnessBindings.Declaration target) {
    if (operand == null || owner.getModule().isNamed() || operand.isVarargsCollector()
        || !owner.getName().replace('.', '/').equals(target.owner())
        || !operand.type().toMethodDescriptorString().equals(route.invokeDescriptor())) return false;
    try {
      var info = MethodHandles.privateLookupIn(owner, MethodHandles.lookup()).revealDirect(operand);
      return info.getDeclaringClass() == owner && info.getName().equals(target.name())
          && info.getMethodType().toMethodDescriptorString().equals(target.descriptor())
          && info.getReferenceKind() == route.referenceKind();
    } catch (IllegalAccessException | IllegalArgumentException unavailable) {
      return false;
    }
  }

  static boolean machinery(StackWalker.StackFrame frame) {
    Class<?> type = frame.getDeclaringClass();
    return type.getClassLoader() == null && type.getModule() == Object.class.getModule()
        && type.getName().startsWith("java.lang.invoke.");
  }
}
