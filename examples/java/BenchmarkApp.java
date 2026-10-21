package examples;

import java.io.BufferedReader;
import java.io.InputStreamReader;

/** Ordinary application code: no telemetry dependencies, annotations or probes. */
public final class BenchmarkApp {
  static final class WorkItem implements AutoCloseable {
    final int seed;
    WorkItem(int seed) { this.seed = seed; }
    @Override public void close() { /* This example owns no external resource. */ }
  }

  static final class LifecycleWorkItem implements AutoCloseable {
    private boolean failClose;
    LifecycleWorkItem(int seed) {
      if (seed < 0) throw new IllegalArgumentException("construction failed");
      failClose = seed == 1;
    }
    @Override public void close() {
      if (failClose) {
        failClose = false;
        throw new IllegalStateException("close failed");
      }
    }
  }

  static void lifecycle() {
    boolean constructionFailed = false;
    boolean closeFailed = false;
    try { new LifecycleWorkItem(-1); }
    catch (IllegalArgumentException error) { constructionFailed = true; }
    LifecycleWorkItem item = new LifecycleWorkItem(1);
    try { item.close(); }
    catch (IllegalStateException error) { closeFailed = true; }
    item.close();
    item.close();
    new LifecycleWorkItem(0); // Deliberately abandoned, to exercise missing close.
    System.out.println("{\"construction_failed\":" + constructionFailed + ",\"close_failed\":" + closeFailed + "}");
  }

  static long workload(int seed) {
    if (seed < 0) throw new IllegalArgumentException("negative seed");
    try (WorkItem item = new WorkItem(seed)) {
      long x = item.seed;
      long sum = 0;
      for (int step = 0; step < 128; step++) {
        x = (x * 1664525 + 1013904223) & 65535;
        sum += x;
      }
      return sum;
    }
  }

  static long excluded(int seed) { return seed; }

  public static void main(String[] args) throws Exception {
    BufferedReader reader = new BufferedReader(new InputStreamReader(System.in));
    for (String line; (line = reader.readLine()) != null;) {
      if (line.equals("quit")) return;
      if (line.equals("lifecycle")) { lifecycle(); continue; }
      if (line.equals("throw")) {
        try { workload(-1); }
        catch (IllegalArgumentException error) {
          System.out.println("{\"exception\":\"IllegalArgumentException\",\"message\":\"" + error.getMessage() + "\"}");
        }
        continue;
      }
      if (line.equals("gc")) { System.gc(); System.out.println("{\"gc_requested\":true}"); continue; }
      int count = Integer.parseInt(line);
      if (count < 1 || count > 1_000_000) throw new IllegalArgumentException("count out of range");
      long start = System.nanoTime();
      long checksum = 0;
      for (int i = 0; i < count; i++) checksum += workload(i) + excluded(0);
      long elapsed = System.nanoTime() - start;
      System.out.println("{\"checksum\":" + checksum + ",\"elapsed_ns\":" + elapsed + ",\"calls\":" + count + "}");
    }
  }
}
