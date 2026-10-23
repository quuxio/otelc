package io.quux.otelc.witness.probe;

import java.util.function.BiConsumer;
import java.util.function.BiFunction;
import java.util.function.Consumer;
import java.util.function.Function;
import java.lang.invoke.MethodHandle;

/** Injected JDK-only bridge. Application inputs and throwable getters never cross it. */
public final class Observer {
  private static final java.util.concurrent.atomic.AtomicReference<BiFunction<Class<?>, String, Object>> START =
      new java.util.concurrent.atomic.AtomicReference<>();
  private static final java.util.concurrent.atomic.AtomicReference<BiConsumer<Object, Throwable>> FINISH =
      new java.util.concurrent.atomic.AtomicReference<>();
  private static final java.util.concurrent.atomic.AtomicReference<Function<Object[], Object>> HANDLE_START =
      new java.util.concurrent.atomic.AtomicReference<>();
  private static final java.util.concurrent.atomic.AtomicReference<Consumer<MethodHandle>> HANDLE =
      new java.util.concurrent.atomic.AtomicReference<>();

  private Observer() {}

  public static synchronized void install(BiFunction<Class<?>, String, Object> enter,
                                          BiConsumer<Object, Throwable> exit) {
    if (START.get() != null) throw new IllegalStateException("witness helper already bound");
    java.util.Objects.requireNonNull(enter);
    FINISH.set(java.util.Objects.requireNonNull(exit));
    START.set(enter);
  }

  public static Object enter(Class<?> owner, String probe) {
    var callback = START.get();
    return callback == null ? null : callback.apply(owner, probe);
  }

  public static Object enter(Class<?> owner, String probe, Class<?> receiver) {
    var handle = HANDLE_START.get();
    if (handle != null) return handle.apply(new Object[]{owner, probe, receiver});
    var callback = START.get();
    return callback == null ? null : callback.apply(owner, probe);
  }

  public static synchronized void installHandle(Consumer<MethodHandle> operand,
                                                Function<Object[], Object> enter) {
    if (START.get() == null || HANDLE_START.get() != null) throw new IllegalStateException("handle helper binding unavailable");
    HANDLE.set(java.util.Objects.requireNonNull(operand));
    HANDLE_START.set(java.util.Objects.requireNonNull(enter));
  }

  public static void handle(MethodHandle operand) {
    try {
      var callback = HANDLE.get();
      if (callback != null) callback.accept(operand);
    } catch (Throwable unavailable) {
      // No proof is published after callback failure. Do not alter the original call.
    }
  }

  public static void exit(Object token, Throwable error) {
    var callback = FINISH.get();
    if (token != null && callback != null) callback.accept(token, error);
  }
}
