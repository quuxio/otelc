package io.quux.otelc.witness.probe;

import java.util.function.BiConsumer;
import java.util.function.BiFunction;

/** Injected JDK-only bridge. Application inputs and throwable getters never cross it. */
public final class Observer {
  private static volatile BiFunction<Class<?>, String, Object> start;
  private static volatile BiConsumer<Object, Throwable> finish;

  private Observer() {}

  public static synchronized void install(BiFunction<Class<?>, String, Object> enter,
                                          BiConsumer<Object, Throwable> exit) {
    if (start != null) throw new IllegalStateException("witness helper already bound");
    finish = java.util.Objects.requireNonNull(exit);
    start = java.util.Objects.requireNonNull(enter);
  }

  public static Object enter(Class<?> owner, String probe) {
    var callback = start;
    return callback == null ? null : callback.apply(owner, probe);
  }

  public static void exit(Object token, Throwable error) {
    var callback = finish;
    if (token != null && callback != null) callback.accept(token, error);
  }
}
