package examples.apps;

public class JavaApp {
  static int process_order(int value) { return value * 3; }
  static int recursive(int depth) { return depth == 0 ? 0 : 1 + recursive(depth - 1); }
  static int caught() { try { throw new IllegalArgumentException("caught"); } catch (IllegalArgumentException error) { return 7; } }
  static int throws_exception() { throw new IllegalArgumentException("escaping"); }
  static class Order {
    final int value;
    Order(int value) { this.value = value; }
    int calculate(int other) { return value + other; }
  }
  public static void main(String[] args) throws Exception {
    int result = process_order(4) + recursive(3) + caught();
    try { throws_exception(); } catch (IllegalArgumentException error) { result += error.getMessage().length(); }
    result += new Order(4).calculate(5);
    var executor = java.util.concurrent.Executors.newFixedThreadPool(2);
    try {
      var first = executor.submit(() -> process_order(5));
      var second = executor.submit(() -> process_order(6));
      result += first.get() + second.get();
    } finally { executor.shutdown(); }
    System.out.println(result);
  }
}
