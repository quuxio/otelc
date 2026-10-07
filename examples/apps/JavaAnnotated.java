package examples.apps;

public class JavaAnnotated {
  @OtelcInstrument
  static int selected(int value) { return value * 3; }
  static int configured(int value) { return value + 7; }
  @OtelcExclude
  static int excluded(int value) { return value - 1; }
  public static void main(String[] args) {
    System.out.println(selected(4) + configured(10) + excluded(2));
  }
}

@interface OtelcInstrument {}
@interface OtelcExclude {}
