package io.quux.otelc.witness.probe;

import java.util.function.BiConsumer;
import java.util.function.BiFunction;

/** Injected JDK-only bridge. Application inputs and throwable getters never cross it. */
public final class Observer {
  private static final java.util.concurrent.atomic.AtomicReference<BiFunction<Class<?>, String, Object>> START =
      new java.util.concurrent.atomic.AtomicReference<>();
  private static final java.util.concurrent.atomic.AtomicReference<BiConsumer<Object, Throwable>> FINISH =
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

  public static void exit(Object token, Throwable error) {
    var callback = FINISH.get();
    if (token != null && callback != null) callback.accept(token, error);
  }
}
