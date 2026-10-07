package examples.apps;

public class JavaTraceApp {
  final int value;
  JavaTraceApp(int value) {
    if (value < 0) throw new IllegalArgumentException("constructor");
    this.value = value;
  }
  static int recursive(int depth) { return depth == 0 ? 0 : 1 + recursive(depth - 1); }
  static int child() { return 42; }
  static int parent() { return child(); }
  static void escaping(Exception error) throws Exception { throw error; }
  public static void main(String[] args) throws Exception {
    if (recursive(3) != 3 || parent() != 42) throw new AssertionError("result");
    var task = Thread.ofVirtual().start(() -> { if (child() != 42) throw new AssertionError("task result"); });
    task.join();
    var error = new IllegalArgumentException("original payload");
    try { escaping(error); throw new AssertionError("missing exception"); }
    catch (Exception original) { if (original != error) throw new AssertionError("exception identity"); }
    if (new JavaTraceApp(7).value != 7) throw new AssertionError("constructor result");
    try { new JavaTraceApp(-1); throw new AssertionError("missing constructor exception"); }
    catch (IllegalArgumentException original) { if (!original.getMessage().equals("constructor")) throw original; }
    System.out.println("trace results preserved");
  }
}
