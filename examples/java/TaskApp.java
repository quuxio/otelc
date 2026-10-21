import java.io.*;
import java.util.concurrent.*;
public final class TaskApp {
  static final ExecutorService POOL = Executors.newSingleThreadExecutor();
  static int getters;
  static final Problem FAILURE = new Problem();
  static final class Problem extends RuntimeException {
    @Override public String toString() { getters++; return "problem"; }
    @Override public String getMessage() { getters++; return "problem"; }
  }
  static int child(int value) { if (value < 0) throw FAILURE; return value + 1; }
  static int cancelledChild(int value) { throw new AssertionError("cancelled work ran"); }
  static int parent(int value) throws Exception { return POOL.submit(() -> child(value)).get(); }
  static boolean cancel() throws Exception {
    CountDownLatch entered = new CountDownLatch(1), release = new CountDownLatch(1);
    Future<?> blocked = POOL.submit(() -> { entered.countDown(); try { release.await(); }
      catch (InterruptedException error) { Thread.currentThread().interrupt(); } });
    entered.await();
    Future<Integer> pending = POOL.submit(() -> cancelledChild(99));
    boolean cancelled = pending.cancel(false);
    release.countDown(); blocked.get(); return cancelled;
  }
  static boolean reject() {
    ExecutorService stopped = Executors.newSingleThreadExecutor(); stopped.shutdown();
    try { stopped.submit(() -> cancelledChild(99)); return false; }
    catch (RejectedExecutionException expected) { return true; }
  }
  public static void main(String[] args) throws Exception {
    BufferedReader input = new BufferedReader(new InputStreamReader(System.in));
    System.out.println("ready");
    try {
      for (String line; (line = input.readLine()) != null && !line.equals("quit");) {
        switch (line) {
          case "run" -> System.out.println(parent(7));
          case "outside" -> System.out.println(POOL.submit(() -> child(20)).get());
          case "cancel" -> System.out.println(cancel());
          case "reject" -> System.out.println(reject());
          case "error" -> {
            try { parent(-1); throw new AssertionError("missing error"); }
            catch (ExecutionException error) { System.out.println((error.getCause() == FAILURE) + ":" + getters); }
          }
          default -> throw new IllegalArgumentException("unknown fixture command");
        }
      }
    } finally { POOL.shutdown(); }
  }
}
