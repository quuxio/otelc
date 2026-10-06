package examples.apps;

public class JavaLatency {
  static int process_order(int value) { return (value * 17 + 3) % 1009; }
  public static void main(String[] args) throws Exception {
    var reader = new java.io.BufferedReader(new java.io.InputStreamReader(System.in));
    System.out.println("ready");
    String line;
    while ((line = reader.readLine()) != null) {
      if (line.equals("quit")) break;
      String[] values = line.split(" ");
      int calls = Integer.parseInt(values[1]);
      if (!values[0].equals("batch") || calls < 1 || calls > 1000000) throw new IllegalArgumentException("invalid batch");
      long start = System.nanoTime();
      long checksum = 0;
      for (int index = 0; index < calls; index++) checksum += process_order(index);
      System.out.printf("elapsed_ns=%d checksum=%d calls=%d%n", System.nanoTime() - start, checksum, calls);
    }
  }
}
