package examples;

import java.io.IOException;

/** Ordinary diagnostic harness; the application and baseline require no telemetry API. */
public final class WitnessHarness {
  private WitnessHarness() {}

  static void rendezvous() throws IOException {
    if (System.in.read() != '\n') throw new IOException("missing harness release");
  }

  static long wrapper(int seed) { return BenchmarkApp.workload(seed); }

  static long envelope(int seed) throws IOException {
    rendezvous();
    return wrapper(seed);
  }

  public static void main(String[] arguments) throws IOException {
    int seed = Integer.parseInt(arguments[0]);
    System.out.println(envelope(seed));
  }
}
