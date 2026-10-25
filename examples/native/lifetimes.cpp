#include <cstdio>

namespace demo {
struct Item {
  int value;
  explicit Item(int input) : value(input) {
    if (input < 0) throw input;
  }
  ~Item() {}
};

int workload(int input) {
  Item local(input);
  return local.value * 3 + 1;
}
}  // namespace demo

int main() {
  int input;
  while (std::scanf("%d", &input) == 1) {
    try {
      std::printf("%d\n", demo::workload(input));
    } catch (int failure) {
      std::printf("caught %d\n", failure);
    }
    std::fflush(stdout);
  }
  return 0;
}
