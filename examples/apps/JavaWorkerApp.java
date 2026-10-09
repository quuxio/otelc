import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.FutureTask;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ThreadPoolExecutor;

public class JavaWorkerApp {
  static final IllegalArgumentException ORIGINAL = new IllegalArgumentException("original");
  static final RejectedExecutionException REJECTED = new RejectedExecutionException("original rejection");
  static FutureTask<Integer> submitted;
  static int child(int value) { if (value < 0) throw ORIGINAL; return value + 1; }
  static FutureTask<Integer> root(ThreadPoolExecutor pool, int value) {
    submitted = new FutureTask<>(() -> child(value));
    pool.execute(submitted);
    return submitted;
  }
  public static void main(String[] args) throws Exception {
    var pool = (ThreadPoolExecutor) Executors.newFixedThreadPool(1);
    try {
      var entered = new CountDownLatch(1); var gate = new CountDownLatch(1);
      var blocker = pool.submit(() -> { entered.countDown(); gate.await(); return 0; });
      entered.await(); var first = root(pool, 10); boolean identity = first == submitted;
      gate.countDown(); blocker.get(); int one = first.get(); int two = root(pool, 20).get();
      boolean sameError = false;
      try { root(pool, -1).get(); } catch (java.util.concurrent.ExecutionException failure) { sameError = failure.getCause() == ORIGINAL; }
      var blocked = new CountDownLatch(1); var release = new CountDownLatch(1);
      var secondBlocker = pool.submit(() -> { blocked.countDown(); release.await(); return 0; });
      blocked.await(); var queued = root(pool, 30); boolean cancelled = queued.cancel(false);
      release.countDown(); secondBlocker.get(); pool.shutdown();
      pool.setRejectedExecutionHandler((task, executor) -> { throw REJECTED; });
      boolean sameRejection = false;
      try { root(pool, 40); } catch (RejectedExecutionException failure) { sameRejection = failure == REJECTED; }
      if (one != 11 || two != 21 || !identity || !sameError || !cancelled || !sameRejection) throw new AssertionError("worker contract");
      System.out.println("results=11,21; original-error=true; cancelled=true; future-identity=true; rejection-identity=true");
    } finally { pool.shutdownNow(); }
  }
}
