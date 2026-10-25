#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <stdexcept>

struct WorkItem {
  int seed;
  explicit WorkItem(int input) : seed(input) {}
  ~WorkItem() {}
};

std::uint64_t workload(int seed) {
  if (seed < 0) throw std::invalid_argument("negative seed");
  WorkItem item(seed);
  std::uint64_t value = item.seed;
  std::uint64_t sum = 0;
  for (int step = 0; step < 128; ++step) {
    value = (value * 1664525 + 1013904223) & 65535;
    sum += value;
  }
  return sum;
}

std::uint64_t excluded(int seed) { return seed; }

int main() {
  char line[128];
  while (std::fgets(line, sizeof(line), stdin)) {
    if (!std::strcmp(line, "quit\n")) return 0;
    int count;
    if (std::sscanf(line, "%d", &count) != 1 || count < 1 || count > 1000000) return 2;
    std::uint64_t checksum = 0;
    auto start = std::chrono::steady_clock::now();
    for (int index = 0; index < count; ++index) checksum += workload(index) + excluded(0);
    auto elapsed = std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now() - start).count();
    std::printf("{\"checksum\":%llu,\"elapsed_ns\":%lld,\"calls\":%d}\n",
                static_cast<unsigned long long>(checksum), static_cast<long long>(elapsed), count);
    std::fflush(stdout);
  }
  return 0;
}
