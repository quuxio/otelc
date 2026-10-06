package quux.otelc.java;

/** Small stable bytecode target; SDK failures never replace application results. */
public final class Probes {
  private Probes() {}
  static volatile Telemetry runtime;
  public static long enter(String name) {
    try { return runtime == null ? 0 : runtime.enter(name); }
    catch (Throwable ignored) { if (runtime != null) runtime.lose("invalid"); return 0; }
  }
  public static void exit(long token, boolean escaped) {
    try { if (runtime != null) runtime.exit(token, escaped); }
    catch (Throwable ignored) { if (runtime != null) runtime.lose("invalid"); }
  }
}
